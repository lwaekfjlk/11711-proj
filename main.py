import os
import torch
import argparse
import time
import csv
import shutil
import evaluate
import torch.distributed as dist
from tqdm import tqdm
from transformers import AutoTokenizer, AutoModelForTokenClassification, AutoConfig
from transformers import pipeline
from transformers import get_cosine_schedule_with_warmup
from dataset import ScirexDataset, SciNERDataset
from torch.utils.data import DataLoader
from torch.nn.parallel import DistributedDataParallel as DDP
from torchcrf import CRF


def load_dataset(args, tokenizer):
    '''
    loading datasets, return a dictionary of dataloaders
    '''
    loader_dict = {}

    if args.train:
        if args.dataset == 'scirex':
            train_dataset = ScirexDataset(args.train_file, tokenizer)
            dev_dataset = ScirexDataset(args.dev_file, tokenizer)
        elif args.dataset == 'sciner':
            train_dataset = SciNERDataset(args.train_file, tokenizer)
            dev_dataset = SciNERDataset(args.dev_file, tokenizer)
        else:
            raise ValueError('Invalid dataset')
        if torch.cuda.device_count() > 1:
            train_sampler = torch.utils.data.distributed.DistributedSampler(train_dataset, num_replicas=dist.get_world_size(), rank=dist.get_rank(), shuffle=True)
            dev_sampler = torch.utils.data.distributed.DistributedSampler(dev_dataset, num_replicas=dist.get_world_size(), rank=dist.get_rank(), shuffle=True)
            train_dataloader = DataLoader(train_dataset, batch_size=args.train_batch_size, sampler=train_sampler, collate_fn=lambda x: train_dataset.collate_fn(x, args.max_length))
            dev_dataloader = DataLoader(dev_dataset, batch_size=args.dev_batch_size, sampler=dev_sampler, collate_fn=lambda x: dev_dataset.collate_fn(x, args.max_length))
        else:
            train_dataloader = DataLoader(train_dataset,  batch_size=args.train_batch_size, shuffle=True, collate_fn=lambda x: train_dataset.collate_fn(x, args.max_length))
            dev_dataloader = DataLoader(dev_dataset, batch_size=args.dev_batch_size, shuffle=True, collate_fn=lambda x: dev_dataset.collate_fn(x, args.max_length))
        loader_dict['train'] = train_dataloader
        loader_dict['dev'] = dev_dataloader

    if args.inference:
        if args.dataset == 'scirex':
            test_dataset = ScirexDataset(args.test_file, tokenizer)
        elif args.dataset == 'sciner':
            test_dataset = SciNERDataset(args.test_file, tokenizer)
        else:
            raise ValueError('Invalid dataset')
        test_dataloader = DataLoader(test_dataset, batch_size=args.test_batch_size, shuffle=False, collate_fn=test_dataset.collate_fn)
        loader_dict['test'] = test_dataloader
    
    return loader_dict


def attach_optimizer(args, model):
    '''
    attach optimizer to the model
    '''
    if args.optimizer_type == 'adamw':
        optimizer = torch.optim.AdamW(model.parameters(), lr=args.learning_rate)
    else:
        raise ValueError('Invalid optimizer')

    return optimizer


def attach_scheduler(args, optimizer, total_training_steps):
    '''
    attach lr scheduler to the model
    '''
    if args.scheduler_type == 'cosine':
        scheduler = get_cosine_schedule_with_warmup(
            optimizer,
            num_warmup_steps=args.warmup_steps,
            num_training_steps=total_training_steps,
        )
        return scheduler
    else:
        raise ValueError('Invalid scheduler type')



def validate(args, dev_dataloader, model, crf_model):
    model.eval()
    label_list = [
        'O',
        'B-MethodName', 'I-MethodName', 'B-HyperparameterName', 'I-HyperparameterName',
        'B-HyperparameterValue', 'I-HyperparameterValue', 'B-MetricName', 'I-MetricName',
        'B-MetricValue', 'I-MetricValue', 'B-TaskName', 'I-TaskName', 'B-DatasetName', 'I-DatasetName',
    ]
    correct_ones = 0
    all_ones = 0
    eval_losses = []
    gth_labels = []
    pred_labels = []
    with torch.no_grad():
        for data in dev_dataloader:
            input_ids = data['input_ids'].to(args.device)
            labels = data['labels'].to(args.device)
            mask_ids = data['attention_mask'].to(args.device)
            outputs = model(input_ids, labels=labels, attention_mask=mask_ids)
            if args.with_crf:
                # incorrect !!!
                crf_emissions = outputs['logits'][:, 1:].contiguous()
                crf_tags = labels[:, 1:].contiguous()
                crf_tags[crf_tags==-100] = 0
                crf_mask = mask_ids[:, 1:].contiguous().byte()
                eval_loss = crf_model.forward(crf_emissions, crf_tags, mask=crf_mask)
                decoded_results = crf_model.decode(crf_emissions, mask=crf_mask)
                predictions = []
                for result in decoded_results:
                    predictions += result
                references = crf_tags.view(-1).tolist()
                masks = crf_mask.view(-1).tolist()
                references = [label for idx,label in enumerate(references) if masks[idx] != 0]
                pred_labels.append(predictions)
                gth_labels.append(references)
            else:
                eval_loss = outputs['loss']
                logits = outputs['logits']
                predictions = torch.argmax(logits, dim=-1)
                pred_labels.append(predictions.view(-1).tolist())
                gth_labels.append(labels.view(-1).tolist())

            eval_losses.append(eval_loss.item()) 
    metric = evaluate.load("seqeval")
    true_predictions = [
        [label_list[p] for (p, l) in zip(pred_label, gth_label) if l != -100]
        for pred_label, gth_label in zip(pred_labels, gth_labels)
    ]
    true_labels = [
        [label_list[l] for (p, l) in zip(pred_label, gth_label) if l != -100]
        for pred_label, gth_label in zip(pred_labels, gth_labels)
    ]
    results = metric.compute(predictions=true_predictions, references=true_labels)
    f1 = results['overall_f1']

    print(f'validation f1 : {f1}')
    eval_loss = sum(eval_losses) / len(eval_losses)
    print(f'validation loss : {eval_loss}')
    model.train()
    return f1, eval_loss


def train(args, model, crf_model, tokenizer):
    best_checkpoint_name = None
    best_eval_f1 = -float('inf')
    best_eval_loss = float('inf')
    global_step = 0
    step = 0
    print('=====begin loading dataset====')
    loaders = load_dataset(args, tokenizer)
    print('=====end loading dataset====')
    train_dataloader = loaders['train']
    dev_dataloader = loaders['dev']
    model.train()
    optimizer = attach_optimizer(args, model)
    total_training_steps = len(train_dataloader) * args.num_epochs // args.gradient_accumulation_step
    scheduler = attach_scheduler(args, optimizer, total_training_steps)

    train_losses = []
    for epoch in range(args.num_epochs):
        for data in tqdm(train_dataloader):
            input_ids = data['input_ids'].to(args.device)
            labels = data['labels'].to(args.device)
            mask_ids = data['attention_mask'].to(args.device)
            outputs = model(input_ids, labels=labels, attention_mask=mask_ids, return_dict=True)
            if args.with_crf:
                # not know how to deal with the [CLS] and [SEP] situation
                crf_emissions = outputs['logits'][:, 1:].contiguous()
                crf_tags = labels[:, 1:].contiguous()
                crf_tags[crf_tags==-100] = 0
                crf_mask = mask_ids[:, 1:].contiguous().byte()
                loss = -crf_model.forward(crf_emissions, crf_tags, mask=crf_mask)
            else:
                loss = outputs['loss']
            loss.backward()
            train_losses.append(loss.item())
            if args.use_wandb:
                wandb.log({'train loss': loss.item()})
            step += 1
            if step % args.gradient_accumulation_step == 0:
                optimizer.step()
                optimizer.zero_grad()
                scheduler.step()
                global_step += 1

                if args.use_wandb:
                    wandb.log({'learning rate': scheduler.get_last_lr()[0], 'step': global_step})

                if global_step % args.evaluation_steps == 0:
                    eval_f1, eval_loss = validate(args, dev_dataloader, model, crf_model)
                    if args.use_wandb:
                        wandb.log({'eval_f1': eval_f1, 'step': global_step})
                        wandb.log({'eval_loss': eval_loss, 'step': global_step})
                    if args.model_chosen_metric == 'f1':
                        if eval_f1 > best_eval_f1:
                            if best_checkpoint_name is not None:
                                os.remove(best_checkpoint_name)
                                if args.with_crf:
                                    os.remove(best_checkpoint_name.replace('.ckpt', '_crf.ckpt'))
                                best_checkpoint_name = args.checkpoint_save_dir + 'best_{}4{}_f1_{}_{}.ckpt'.format(args.model_name.split('/')[-1], args.task, round(eval_f1*100,3), args.timestamp)
                            else:
                                best_checkpoint_name = args.checkpoint_save_dir + 'best_{}4{}_f1_{}_{}.ckpt'.format(args.model_name.split('/')[-1], args.task, round(eval_f1*100,3), args.timestamp)
                            model_to_save = model.module if hasattr(model, 'module') else model
                            output_model_file = best_checkpoint_name
                            torch.save(model_to_save.state_dict(), output_model_file)
                            if args.with_crf:
                                crf_model_to_save = crf_model.module if hasattr(crf_model, 'module') else crf_model
                                output_crf_model_file = best_checkpoint_name.replace('.ckpt', '_crf.ckpt')
                                torch.save(crf_model_to_save.state_dict(), output_crf_model_file)
                            best_eval_f1 = eval_f1
                    elif args.model_chosen_metric == 'loss':
                        if eval_loss < best_eval_loss:
                            if best_checkpoint_name is not None:
                                os.remove(best_checkpoint_name)
                                if args.with_crf:
                                    os.remove(best_checkpoint_name.replace('.ckpt', '_crf.ckpt'))
                                best_checkpoint_name = args.checkpoint_save_dir + 'best_{}4{}_loss_{}_{}.ckpt'.format(args.model_name.split('/')[-1], args.task, round(eval_loss,3), args.timestamp)
                            else:
                                best_checkpoint_name = args.checkpoint_save_dir + 'best_{}4{}_loss_{}_{}.ckpt'.format(args.model_name.split('/')[-1], args.task, round(eval_loss,3), args.timestamp)
                            model_to_save = model.module if hasattr(model, 'module') else model
                            output_model_file = best_checkpoint_name
                            torch.save(model_to_save.state_dict(), output_model_file)
                            if args.with_crf:
                                crf_model_to_save = crf_model.module if hasattr(crf_model, 'module') else crf_model
                                output_crf_model_file = best_checkpoint_name.replace('.ckpt', '_crf.ckpt')
                                torch.save(crf_model_to_save.state_dict(), output_crf_model_file)
                            best_eval_loss = eval_loss
                    else:
                        raise NotImplementedError
        epoch_loss = sum(train_losses) / len(train_losses)
        print(f'Epoch {epoch} loss: {epoch_loss}')

    src_file = best_checkpoint_name
    tgt_file = args.checkpoint_save_dir + 'best_{}4{}.ckpt'.format(args.model_name.split('/')[-1], args.task)
    if args.with_crf:
        src_crf_file = best_checkpoint_name.replace('.ckpt', '_crf.ckpt')
        tgt_crf_file = args.checkpoint_save_dir + 'best_{}4{}_crf.ckpt'.format(args.model_name.split('/')[-1], args.task)
        shutil.copy(src_crf_file, tgt_crf_file)
    shutil.copy(src_file, tgt_file)
    return


def test(args, model, tokenizer):
    raise NotImplementedError


def sciner_inference(args, model, crf_model, tokenizer):
    entities = [
        'O',
        'B-MethodName', 'I-MethodName', 'B-HyperparameterName', 'I-HyperparameterName',
        'B-HyperparameterValue', 'I-HyperparameterValue', 'B-MetricName', 'I-MetricName',
        'B-MetricValue', 'I-MetricValue', 'B-TaskName', 'I-TaskName', 'B-DatasetName', 'I-DatasetName',
    ]

    model.load_state_dict(torch.load(args.checkpoint_save_dir + 'best_{}4{}.ckpt'.format(args.model_name.split('/')[-1], args.task)))
    if args.with_crf:
        crf_model.load_state_dict(torch.load(args.checkpoint_save_dir + 'best_{}4{}_crf.ckpt'.format(args.model_name.split('/')[-1], args.task)))
    id2entity = {i: e for i, e in enumerate(entities)}
    label2id = model.config.label2id

    ner_pipeline = pipeline("ner", model=model, tokenizer=tokenizer, device=0)
    with open(args.output_file, 'w', newline='') as output_f, open(args.inference_file, 'r') as input_f:
        sents = input_f.readlines()
        for sent in tqdm(sents):
            tokenized_sent = tokenizer.tokenize(sent)
            input_ids = tokenizer.convert_tokens_to_ids(tokenizer.tokenize(sent))
            input_ids = torch.tensor([tokenizer.cls_token_id] + input_ids + [tokenizer.sep_token_id]).unsqueeze(0).to(args.device)
            outputs = model(input_ids=input_ids)
            if args.with_crf:
                crf_emissions = outputs['logits'][:, 1:].contiguous()
                crf_mask = torch.ones(crf_emissions.shape[:2], dtype=torch.bool).to(args.device)
                decoded_results = crf_model.decode(crf_emissions, mask=crf_mask)
                target_words = sent.strip().split(' ')
                words = []
                entities = []
                for idx, subword in enumerate(tokenized_sent):
                    if subword.startswith('##'):
                        words[-1] += subword[2:]
                    else:
                        words.append(subword)
                        entities.append(id2entity[decoded_results[0][idx]])
                start_idx = 0
                final_words = []
                final_entities = []
                for t_w in target_words:
                    subword = words[start_idx]
                    entity = entities[start_idx]
                    if subword == '[UNK]':
                        subword = t_w[0]
                    while len(subword) < len(t_w):
                        start_idx += 1
                        subword += words[start_idx]
                    final_words.append(t_w)
                    final_entities.append(entity)
                    start_idx += 1
                for w, e in zip(final_words, final_entities):
                    output_f.write('{}\t{}\n'.format(w, e))
                output_f.write('\n')
            else:
                target_words = sent.strip().split(' ')
                ner_res = ner_pipeline(sent)
                words = []
                entities = []
                ner_index = 0
                target_index = 0
                while ner_index < len(ner_res):
                    sub_word = ner_res[ner_index]['word']
                    sub_word = sub_word.replace('##', '')
                    entity = ner_res[ner_index]['entity']
                    entity = id2entity[label2id[entity]]
                    words.append(sub_word)
                    entities.append(entity)
                    ner_index += 1
                    target_index += 1
                    match_word = tokenizer.decode(tokenizer.encode(target_words[target_index-1]), skip_special_tokens=True)
                    match_sub_word = tokenizer.decode(tokenizer.encode(words[-1]), skip_special_tokens=True)
                    while match_sub_word != match_word:
                        sub_word = ner_res[ner_index]['word']
                        words[-1] += sub_word.replace('##', '')
                        ner_index += 1
                        match_sub_word = tokenizer.decode(tokenizer.encode(words[-1]), skip_special_tokens=True)
                    output_f.write(target_words[target_index-1]+'\t'+entities[-1]+'\n')
                output_f.write('\n')
    return


def distributed_setup(args, model):
    '''
    setup distributed training
    '''
    torch.cuda.set_device(args.local_rank)
    torch.distributed.init_process_group(backend='nccl')
    args.device = torch.device('cuda', args.local_rank)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--model_name', type=str, default='allenai/scibert_scivocab_uncased', help='model name or path')
    parser.add_argument('--train_file', type=str, default='./data/sciner_dataset/train.conll', help='path to train file, jsonl for scirex, conll for sciner')
    parser.add_argument('--dev_file', type=str, default='./data/sciner_dataset/validation.conll', help='path to dev file')
    parser.add_argument('--test_file', type=str, default='./data/sciner_dataset/validation.conll', help='path to test file')
    parser.add_argument('--inference_file', type=str, default='./data/anlp_test/anlp-sciner-test.txt', help='final ANLP submission file')
    parser.add_argument('--output_file', type=str, default='./data/anlp_test/anlp_haofeiy_sciner.conll')
    parser.add_argument('--task', type=str, default='sciner-finetune', choices=['sciner-finetune', 'scirex-finetune'])
    parser.add_argument('--load_from_checkpoint', type=str, default=None, help='contine finetuning based on one checkpoint')
    parser.add_argument('--model_chosen_metric', type=str, default='f1', help='choose dev checkpoint based on this metric')
    parser.add_argument('--checkpoint_save_dir', type=str, default='./checkpoints/')
    parser.add_argument('--train_batch_size', type=int, default=4)
    parser.add_argument('--gradient_accumulation_step', type=int, default=4)
    parser.add_argument('--dev_batch_size', type=int, default=4)
    parser.add_argument('--test_batch_size', type=int, default=4)
    parser.add_argument('--max_length', type=int, default=512)
    parser.add_argument('--num_epochs', type=int, default=10)
    parser.add_argument('--learning_rate', type=float, default=5e-5)
    parser.add_argument('--optimizer_type', type=str, default='adamw')
    parser.add_argument('--scheduler_type', type=str, default='cosine')
    parser.add_argument('--weight_decay', type=float, default=0.0)
    parser.add_argument('--warmup_steps', type=int, default=0)
    parser.add_argument('--seed', type=int, default=42)
    parser.add_argument('--device', type=str, default='cuda')
    parser.add_argument('--dataset', type=str, default='sciner')
    parser.add_argument('--label_num', type=int, default=15, help='number of labels, 15 for sciner, 9 for scirex')
    parser.add_argument('--train', action='store_true')
    parser.add_argument('--inference', action='store_true')
    parser.add_argument('--local_rank', type=int, default=-1)
    parser.add_argument('--evaluation_steps', type=int, default=50)
    parser.add_argument('--use_wandb', action='store_true')
    parser.add_argument('--with_crf', action='store_true')

    args = parser.parse_args()
    if torch.cuda.device_count() > 1:
        args.local_rank = int(os.environ['LOCAL_RANK'])
    args.timestamp = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(int(round(time.time()*1000))/1000))

    if args.use_wandb:
        import wandb
        # need to change to your own API when using
        os.environ['EXP_NUM'] = 'SciNER'
        os.environ['WANDB_NAME'] = time.strftime(
            '%Y-%m-%d %H:%M:%S', 
            time.localtime(int(round(time.time()*1000))/1000)
        )
        os.environ['WANDB_API_KEY'] = '972035264241fb0f6cc3cab51a5d82f47ca713db'
        os.environ['WANDB_DIR'] = './SciNER_tmp'
        wandb.init(project="SciNER")

    tokenizer = AutoTokenizer.from_pretrained(args.model_name)
    config = AutoConfig.from_pretrained(args.model_name, num_labels=args.label_num)
    model = AutoModelForTokenClassification.from_pretrained(args.model_name, config=config, ignore_mismatched_sizes=True)

    device = torch.device(args.local_rank) if args.local_rank != -1 else torch.device('cuda')
    
    if args.with_crf:
        crf_model = CRF(args.label_num, batch_first=True).to(args.device)
    else:
        crf_model = None
    
    if args.load_from_checkpoint:
        model_dict = torch.load(args.load_from_checkpoint)
        filtered_model_dict = {k: v for k, v in model_dict.items() if 'classifier' not in k}
        model_dict.update(filtered_model_dict)
        model.load_state_dict(filtered_model_dict, strict=False)
        if args.with_crf:
            crf_dict = torch.load(args.load_from_checkpoint.replace('.ckpt', '_crf.ckpt'))
            crf_model.load_state_dict(crf_dict)
    
    model.to(device)
    
    if torch.cuda.device_count() > 1:
        distributed_setup(args, model)
        model = torch.nn.parallel.DistributedDataParallel(model, device_ids=[args.local_rank], output_device=args.local_rank)

    if args.train:
        train(args, model, crf_model, tokenizer)
    elif args.inference:
        conll_result = sciner_inference(args, model, crf_model, tokenizer)

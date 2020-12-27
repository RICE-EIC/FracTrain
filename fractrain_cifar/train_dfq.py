"""
Training file for training SkipNets for supervised pre-training stage
"""

from __future__ import print_function

import torch
import torch.nn as nn
import torch.backends.cudnn as cudnn
from torch.autograd import Variable

import os
import shutil
import argparse
import time
import logging
import numpy as np
import json

import models
from data import *

import util_swa

from functools import reduce


model_names = sorted(name for name in models.__dict__
                     if name.islower() and not name.startswith('__')
                     and callable(models.__dict__[name])
                     )


def parse_args():
    # hyper-parameters are from ResNet paper
    parser = argparse.ArgumentParser(
        description='PyTorch CIFAR10 training with gating')
    parser.add_argument('--dir', help='annotate the working directory')
    parser.add_argument('--cmd', choices=['train', 'test'], default='train')
    parser.add_argument('--arch', metavar='ARCH',
                        default='cifar10_rnn_gate_38',
                        choices=model_names,
                        help='model architecture: ' +
                             ' | '.join(model_names) +
                             ' (default: cifar10_feedforward_38)')
    parser.add_argument('--gate_type', type=str, default='ff',
                        choices=['ff', 'rnn'], help='gate type')
    parser.add_argument('--dataset', '-d', default='cifar10', type=str,
                        choices=['cifar10', 'cifar100'],
                        help='dataset type')
    parser.add_argument('--datadir', default='/home/yf22/dataset', type=str,
                        help='path to dataset')
    parser.add_argument('--workers', default=4, type=int, metavar='N',
                        help='number of data loading workers (default: 4 )')
    parser.add_argument('--iters', default=64000, type=int,
                        help='number of total iterations (default: 64,000)')
    parser.add_argument('--start_iter', default=0, type=int,
                        help='manual iter number (useful on restarts)')
    parser.add_argument('--batch_size', default=128, type=int,
                        help='mini-batch size (default: 128)')
    parser.add_argument('--lr_schedule', default='piecewise', type=str,
                        help='learning rate schedule')
    parser.add_argument('--lr', default=0.1, type=float,
                        help='initial learning rate')
    parser.add_argument('--momentum', default=0.9, type=float,
                        help='momentum')
    parser.add_argument('--weight_decay', default=1e-4, type=float,
                        help='weight decay (default: 1e-4)')
    parser.add_argument('--print_freq', default=10, type=int,
                        help='print frequency (default: 10)')
    parser.add_argument('--resume', default='', type=str,
                        help='path to  latest checkpoint (default: None)')
    parser.add_argument('--pretrained', dest='pretrained', action='store_true',
                        help='use pretrained model')
    parser.add_argument('--step_ratio', default=0.1, type=float,
                        help='ratio for learning rate deduction')
    parser.add_argument('--warm_up', action='store_true',
                        help='for n = 18, the model needs to warm up for 400 '
                             'iterations')
    parser.add_argument('--save_folder', default='save_checkpoints',
                        type=str,
                        help='folder to save the checkpoints')
    parser.add_argument('--eval_every', default=390, type=int,
                        help='evaluate model every (default: 1000) iterations')
    parser.add_argument('--verbose', action="store_true",
                        help='print layer skipping ratio at training')
    parser.add_argument('--target_ratio', default=100, type=float,
                        help='target_ratio')
    parser.add_argument('--computation_loss', default=True, type=bool,
                        help='using computation loss as regularization term')
    parser.add_argument('--proceed', default='False',
                        help='whether this experiment continues from a checkpoint')
    parser.add_argument('--beta', default=1e-3, type=float,
                        help='coefficient')
    parser.add_argument('--beta_decay', default=1, type=float,
                        help='decay of beta') 
    parser.add_argument('--ada_beta', default=False, action='store_true',
                        help='adaptively change beta')
    parser.add_argument('--rnn_initial', default=False, action='store_true',
                        help='whether to initialize rnn to choose full precisioin')
    parser.add_argument('--act_fw', default=0, type=int,
                        help='precision of activation during forward, -1 means dynamic, 0 means no quantize')
    parser.add_argument('--act_bw', default=0, type=int,
                        help='precision of activation during backward, -1 means dynamic, 0 means no quantize')
    parser.add_argument('--grad_act_error', default=0, type=int,
                        help='precision of activation gradient during error backward, -1 means dynamic, 0 means no quantize')
    parser.add_argument('--grad_act_gc', default=0, type=int,
                        help='precision of activation gradient during weight gradient computation, -1 means dynamic, 0 means no quantize')
    parser.add_argument('--weight_bits', default=0, type=int,
                        help='precision of weight')
    parser.add_argument('--schedule', default=None, type=int, nargs='*',
                        help='target ratio schedule')
    parser.add_argument('--target_ratio_schedule',default=None,type=float,nargs='*',
                        help='schedule for target compression ratio')
    parser.add_argument('--weight_bits_schedule',default=None,type=float,nargs='*',
                        help='schedule for weight precision')
    parser.add_argument('--momentum_act', default=0.9, type=float,
                        help='momentum for act min/max')   
    parser.add_argument('--relax', default=0, type=float,
                        help='relax parameter for target ratio') 
    parser.add_argument('--loss_sf', default=None, type=float,
                        help='loss scale factor during backward')
    parser.add_argument('--finetune_step', default=0, type=int,
                    help='num steps to finetune with full precision')
    parser.add_argument('--conv_info', default='', type=str,
                    help='load the layerwise flops information')
    parser.add_argument('--dws_bits', default=8, type=int,
                    help='precision for dws conv weight and activation')
    parser.add_argument('--dws_grad_bits', default=16, type=int,
                    help='precision for dws conv error and gradient')
    parser.add_argument('--swa_start', type=float, default=None, help='SWA start step number')
    parser.add_argument('--swa_freq', type=float, default=1170,
                        help='SWA model collection frequency')
    args = parser.parse_args()
    return args

args = parse_args()

def main():
    models.ACT_FW = args.act_fw
    models.ACT_BW = args.act_bw
    models.GRAD_ACT_ERROR = args.grad_act_error
    models.GRAD_ACT_GC = args.grad_act_gc
    models.WEIGHT_BITS = args.weight_bits
    models.MOMENTUM = args.momentum_act

    models.DWS_BITS = args.dws_bits
    models.DWS_GRAD_BITS = args.dws_grad_bits
    
    save_path = args.save_path = os.path.join(args.save_folder, args.arch)
    os.makedirs(save_path, exist_ok=True)

    # config logger file
    args.logger_file = os.path.join(save_path, 'log_{}.txt'.format(args.cmd))
    handlers = [logging.FileHandler(args.logger_file, mode='w'),
                logging.StreamHandler()]
    logging.basicConfig(level=logging.INFO,
                        datefmt='%m-%d-%y %H:%M',
                        format='%(asctime)s:%(message)s',
                        handlers=handlers)

    if args.cmd == 'train':
        logging.info('start training {}'.format(args.arch))
        run_training(args)

    elif args.cmd == 'test':
        logging.info('start evaluating {} with checkpoints from {}'.format(
            args.arch, args.resume))
        test_model(args)


def fix_rnn(model):    
    for param in model.control.parameters():
        param.requires_grad = False

    for param in model.control_grad.parameters():
        param.requires_grad = False
        
    for g in range(3):
        for i in range(model.num_layers[g]):
            gate_layer = getattr(model,'group{}_gate{}'.format(g + 1,i))
            for param in gate_layer.parameters():
                param.requires_grad = False

bits = [3, 4, 4, 6, 6]
grad_bits = [6, 6, 8, 8, 12]


if args.conv_info or 'mobilenet' in args.arch:
    conv_info = np.load(args.conv_info, allow_pickle=True).item()['conv']

    dws_info = np.load(args.conv_info, allow_pickle=True).item()['dws']
    dws_flops_fw = sum(dws_info) * args.dws_bits * args.dws_bits /32 /32
    dws_flops_gc = dws_flops_eb = sum(dws_info) * args.dws_bits * args.dws_grad_bits /32 /32 
    dws_flops_total = dws_flops_fw + dws_flops_eb + dws_flops_gc

else:
    conv_info = None
    dws_flops_total = dws_flops_fw = dws_flops_gc = dws_flops_eb = 0


def run_training(args):
    global conv_info

    cost_fw = []
    for bit in bits:
        cost_fw.append(bit/32)
    cost_fw = np.array(cost_fw) * args.weight_bits/32

    cost_eb = []
    for bit in grad_bits:
        cost_eb.append(bit/32)
    cost_eb = np.array(cost_eb) * args.weight_bits/32

    cost_gc = []
    for i in range(len(bits)):
        cost_gc.append(bits[i] * grad_bits[i]/32/32)
    cost_gc = np.array(cost_gc)

    # create model
    model = models.__dict__[args.arch](args.pretrained, proj_dim=len(bits))
    model = torch.nn.DataParallel(model).cuda()

    if args.swa_start is not None:
        print('SWA training')
        swa_model = torch.nn.DataParallel(models.__dict__[args.arch](args.pretrained, proj_dim=len(bits))).cuda()
        swa_n = 0

    else:
        print('SGD training')

    best_prec1 = 0
    best_iter = 0
    # best_full_prec = 0

    best_swa_prec = 0
    best_swa_iter = 0

    # optionally resume from a checkpoint
    if args.resume:
        if os.path.isfile(args.resume):
            logging.info('=> loading checkpoint `{}`'.format(args.resume))
            checkpoint = torch.load(args.resume)
            if args.proceed == 'True':
                args.start_iter = checkpoint['iter']
            else:
                args.start_iter = 0
            best_prec1 = checkpoint['best_prec1']
            model.load_state_dict(checkpoint['state_dict'],strict=True)
            logging.info('=> loaded checkpoint `{}` (iter: {})'.format(
                args.resume, checkpoint['iter']
            ))

            if args.swa_start is not None:
                swa_state_dict = checkpoint['swa_state_dict']
                if swa_state_dict is not None:
                    swa_model.load_state_dict(swa_state_dict)    
                swa_n_ckpt = checkpoint['swa_n']
                if swa_n_ckpt is not None:
                    swa_n = swa_n_ckpt
                best_swa_prec_ckpt = checkpoint['best_swa_prec']
                if best_swa_prec_ckpt is not None:
                    best_swa_prec = best_swa_prec_ckpt

        else:
            logging.info('=> no checkpoint found at `{}`'.format(args.resume))

    cudnn.benchmark = True

    train_loader = prepare_train_data(dataset=args.dataset,
                                      datadir=args.datadir,
                                      batch_size=args.batch_size,
                                      shuffle=True,
                                      num_workers=args.workers)
    test_loader = prepare_test_data(dataset=args.dataset,
                                    datadir=args.datadir,
                                    batch_size=args.batch_size,
                                    shuffle=False,
                                    num_workers=args.workers)

    if args.swa_start is not None:
        swa_loader = prepare_train_data(dataset=args.dataset,
                                      datadir=args.datadir,
                                      batch_size=args.batch_size,
                                      shuffle=False,
                                      num_workers=args.workers)

    if args.rnn_initial:
        for param in model.parameters():
            param.requires_grad = False
        
        for param in model.control.parameters():
            param.requires_grad = True

        for param in model.control_grad.parameters():
            param.requires_grad = True
            
        for g in range(3):
            for i in range(model.num_layers[g]):
                gate_layer = getattr(model,'group{}_gate{}'.format(g + 1,i))
                for param in gate_layer.parameters():
                    param.requires_grad = True

    # define loss function (criterion) and optimizer
    criterion = nn.CrossEntropyLoss().cuda()

    optimizer = torch.optim.SGD(model.parameters(),
                                args.lr,
                                momentum=args.momentum,
                                weight_decay=args.weight_decay)

    batch_time = AverageMeter()
    data_time = AverageMeter()
    losses = AverageMeter()
    top1 = AverageMeter()
    skip_ratios = ListAverageMeter()
    cp_record = AverageMeter()
    cp_record_fw = AverageMeter()
    cp_record_eb = AverageMeter()
    cp_record_gc = AverageMeter()
    
    network_depth = sum(model.module.num_layers)

    if conv_info is None:
        conv_info = [1 for _ in range(network_depth)]

    layerwise_decision_statistics = []
    
    for k in range(network_depth):
        layerwise_decision_statistics.append([])
        for j in range(len(cost_fw)):
            ratio = AverageMeter()
            layerwise_decision_statistics[k].append(ratio)

    end = time.time()

    i = args.start_iter
    while i < args.iters + args.finetune_step:
        for input, target in train_loader:
            # measuring data loading time
            data_time.update(time.time() - end)

            model.train()
            # adjust_learning_rate(args, optimizer1, optimizer2, i)
            adjust_learning_rate(args, optimizer, i)
            adjust_target_ratio(args, i)
            i += 1

            target = target.cuda()
            input_var = Variable(input).cuda()
            target_var = Variable(target).cuda()
           
            if i > args.iters:
                output, _ = model(input_var, np.zeros(len(bits)), np.zeros(len(grad_bits)))
                computation_cost = 0
                cp_ratio = 1
                cp_ratio_fw = 1
                cp_ratio_eb = 1
                cp_ratio_gc = 1

            else:
                output, masks = model(input_var, bits, grad_bits)
                
                computation_cost_fw = 0
                computation_cost_eb = 0
                computation_cost_gc = 0
                
                for layer in range(network_depth):
                    
                    full_layer = reduce((lambda x, y: x * y), masks[layer][0].shape)
                    
                    for k in range(len(cost_fw)):
                        
                        dynamic_choice = masks[layer][k].sum()
                        
                        ratio = dynamic_choice / full_layer

                        layerwise_decision_statistics[layer][k].update(ratio.data, 1)
                        
                        computation_cost_fw += masks[layer][k].sum() * cost_fw[k] * conv_info[layer]
                        computation_cost_eb += masks[layer][k].sum() * cost_eb[k] * conv_info[layer]
                        computation_cost_gc += masks[layer][k].sum() * cost_gc[k] * conv_info[layer]
                
                computation_cost_fw += dws_flops_fw * args.batch_size
                computation_cost_eb += dws_flops_eb * args.batch_size
                computation_cost_gc += dws_flops_gc * args.batch_size

                computation_cost = computation_cost_fw + computation_cost_eb + computation_cost_gc

                cp_ratio_fw = float(computation_cost_fw) / args.batch_size / (sum(conv_info) + dws_flops_fw) * 100
                cp_ratio_eb = float(computation_cost_eb) / args.batch_size / (sum(conv_info) + dws_flops_eb) * 100
                cp_ratio_gc = float(computation_cost_gc) / args.batch_size / (sum(conv_info) + dws_flops_gc) * 100

                cp_ratio = float(computation_cost) / args.batch_size / (sum(conv_info)*3 + dws_flops_total) * 100
                    
                computation_loss = computation_cost / np.mean(conv_info) * args.beta
            
            if cp_ratio < args.target_ratio - args.relax:
                reg = -1
            elif cp_ratio >= args.target_ratio + args.relax:
                reg = 1
            elif cp_ratio >=args.target_ratio:
                reg = 0.1
            else:
                reg = -0.1
            
            loss_cls = criterion(output, target_var)

            if computation_loss > loss_cls/10 and args.ada_beta: 
                computation_loss *= loss_cls.detach()/10/computation_loss.detach()

            if args.computation_loss:
                loss = loss_cls + computation_loss * reg
            else:
                loss = loss_cls

            # measure accuracy and record loss
            prec1, = accuracy(output.data, target, topk=(1,))
            losses.update(loss.item(), input.size(0))
            top1.update(prec1.item(), input.size(0))
            # skip_ratios.update(skips, input.size(0))
            cp_record.update(cp_ratio,1)
            cp_record_fw.update(cp_ratio_fw,1)
            cp_record_eb.update(cp_ratio_eb,1)
            cp_record_gc.update(cp_ratio_gc,1)

            optimizer.zero_grad()

            if args.loss_sf:
                loss *= args.loss_sf

            loss.backward()

            if args.loss_sf:
                for param in model.parameters():
                    if param.requires_grad and param.grad is not None:
                        param.grad.data /= args.loss_sf

            optimizer.step()

            # repackage hidden units for RNN Gate
            if args.gate_type == 'rnn':
                model.module.control.repackage_hidden()

            batch_time.update(time.time() - end)
            end = time.time()

            # print log
            if i % args.print_freq == 0 or i == (args.iters - 1):
                logging.info("Iter: [{0}/{1}]\t"
                             "Time {batch_time.val:.3f} ({batch_time.avg:.3f})\t"
                             "Data {data_time.val:.3f} ({data_time.avg:.3f})\t"
                             "Loss {loss.val:.3f} ({loss.avg:.3f})\t"
                             "Prec@1 {top1.val:.3f} ({top1.avg:.3f})\t"
                             "Computation_Percentage: {cp_record.val:.3f}({cp_record.avg:.3f})\t"
                             "Computation_Percentage_FW: {cp_record_fw.val:.3f}({cp_record_fw.avg:.3f})\t"
                             "Computation_Percentage_EB: {cp_record_eb.val:.3f}({cp_record_eb.avg:.3f})\t"
                             "Computation_Percentage_GC: {cp_record_gc.val:.3f}({cp_record_gc.avg:.3f})\t".format(
                                i,
                                args.iters,
                                batch_time=batch_time,
                                data_time=data_time,
                                loss=losses,
                                top1=top1,
                                cp_record=cp_record,
                                cp_record_fw=cp_record_fw,
                                cp_record_eb=cp_record_eb,
                                cp_record_gc=cp_record_gc)
                )
            
            if args.swa_start is not None and i >= args.swa_start and i % args.swa_freq == 0:
                util_swa.moving_average(swa_model, model, 1.0 / (swa_n + 1))
                swa_n += 1
                util_swa.bn_update(swa_loader, swa_model, bits, grad_bits)

                with torch.no_grad():
                    prec1 = validate(args, test_loader, swa_model, criterion, i, swa=True)

                if prec1 > best_swa_prec:
                    best_swa_prec = prec1
                    best_swa_iter = i

                print("Current Best SWA Prec@1: ", best_swa_prec)
                print("Current Best SWA Iteration: ", best_swa_iter)

            if (i % args.eval_every == 0 and i > 0) or (i == args.iters):
                
                with torch.no_grad():
                    prec1 = validate(args, test_loader, model, criterion, i)
                    # prec_full = validate_full_prec(args, test_loader, model, criterion, i)

                is_best = prec1 > best_prec1
                if is_best:
                    best_prec1 = prec1
                    best_iter = i

                # best_full_prec = max(prec_full, best_full_prec)

                print("Current Best Prec@1: ", best_prec1)
                print("Current Best Iteration: ", best_iter)

                checkpoint_path = os.path.join(args.save_path, 'checkpoint_{:05d}_{:.2f}.pth.tar'.format(i, prec1))
                save_checkpoint({
                    'iter': i,
                    'arch': args.arch,
                    'state_dict': model.state_dict(),
                    'best_prec1': best_prec1,
                    'swa_state_dict' : swa_model.state_dict() if args.swa_start is not None else None,
                    'swa_n' : swa_n if args.swa_start is not None else None,
                    'best_swa_prec' : best_swa_prec if args.swa_start is not None else None,
                },
                    is_best = is_best, filename=checkpoint_path)
                shutil.copyfile(checkpoint_path, os.path.join(args.save_path,
                                                              'checkpoint_latest'
                                                              '.pth.tar'))

            if i >= args.iters + args.finetune_step:
                break


def validate(args, test_loader, model, criterion, step, swa=False):
    global conv_info

    cost_fw = []
    for bit in bits:
        cost_fw.append(bit/32)
    cost_fw = np.array(cost_fw) * args.weight_bits/32

    cost_eb = []
    for bit in grad_bits:
        cost_eb.append(bit/32)
    cost_eb = np.array(cost_eb) * args.weight_bits/32

    cost_gc = []
    for i in range(len(bits)):
        cost_gc.append(bits[i] * grad_bits[i]/32/32)
    cost_gc = np.array(cost_gc)

    batch_time = AverageMeter()
    data_time = AverageMeter()
    losses = AverageMeter()
    top1 = AverageMeter()
    skip_ratios = ListAverageMeter()
    cp_record = AverageMeter()
    cp_record_fw = AverageMeter()
    cp_record_eb = AverageMeter()
    cp_record_gc = AverageMeter()
    
    network_depth = sum(model.module.num_layers)

    layerwise_decision_statistics = []
    
    for k in range(network_depth):
        layerwise_decision_statistics.append([])
        for j in range(len(cost_fw)):
            ratio = AverageMeter()
            layerwise_decision_statistics[k].append(ratio)

    model.eval()
    end = time.time()
    for i, (input, target) in enumerate(test_loader):
        data_time.update(time.time() - end)

        target = target.cuda()
        input_var = Variable(input).cuda()
        target_var = Variable(target).cuda()
       
        output, masks = model(input_var, bits, grad_bits)
        
        computation_cost_fw = 0
        computation_cost_eb = 0
        computation_cost_gc = 0
        computation_all = 0
            
        for layer in range(network_depth):
            
            full_layer = reduce((lambda x, y: x * y), masks[layer][0].shape)
            
            for k in range(len(cost_fw)):
                
                dynamic_choice = masks[layer][k].sum()
                
                ratio = dynamic_choice / full_layer

                layerwise_decision_statistics[layer][k].update(ratio.data, 1)
                
                computation_cost_fw += masks[layer][k].sum() * cost_fw[k] * conv_info[layer]
                computation_cost_eb += masks[layer][k].sum() * cost_eb[k] * conv_info[layer]
                computation_cost_gc += masks[layer][k].sum() * cost_gc[k] * conv_info[layer]
        
        computation_cost_fw += dws_flops_fw * args.batch_size
        computation_cost_eb += dws_flops_eb * args.batch_size
        computation_cost_gc += dws_flops_gc * args.batch_size

        computation_cost = computation_cost_fw + computation_cost_eb + computation_cost_gc

        cp_ratio_fw = float(computation_cost_fw) / args.batch_size / (sum(conv_info) + dws_flops_fw) * 100
        cp_ratio_eb = float(computation_cost_eb) / args.batch_size / (sum(conv_info) + dws_flops_eb) * 100
        cp_ratio_gc = float(computation_cost_gc) / args.batch_size / (sum(conv_info) + dws_flops_gc) * 100

        cp_ratio = float(computation_cost) / args.batch_size / (sum(conv_info)*3 + dws_flops_total) * 100         
            
        loss = criterion(output, target_var)

        # measure accuracy and record loss
        prec1, = accuracy(output.data, target, topk=(1,))
        losses.update(loss.item(), input.size(0))
        top1.update(prec1.item(), input.size(0))
        # skip_ratios.update(skips, input.size(0))
        cp_record.update(cp_ratio,1)
        cp_record_fw.update(cp_ratio_fw,1)
        cp_record_eb.update(cp_ratio_eb,1)
        cp_record_gc.update(cp_ratio_gc,1)

        # repackage hidden units for RNN Gate
        if args.gate_type == 'rnn':
            model.module.control.repackage_hidden()

        batch_time.update(time.time() - end)
        end = time.time()

        # print log
        if i % args.print_freq == 0 or (i == (len(test_loader) - 1)):
            logging.info("Iter: [{0}/{1}]\t"
                         "Time {batch_time.val:.3f} ({batch_time.avg:.3f})\t"
                         "Data {data_time.val:.3f} ({data_time.avg:.3f})\t"
                         "Loss {loss.val:.3f} ({loss.avg:.3f})\t"
                         "Prec@1 {top1.val:.3f} ({top1.avg:.3f})\t"
                         "Computation_Percentage: {cp_record.val:.3f}({cp_record.avg:.3f})\t"
                         "Computation_Percentage_FW: {cp_record_fw.val:.3f}({cp_record_fw.avg:.3f})\t"
                         "Computation_Percentage_EB: {cp_record_eb.val:.3f}({cp_record_eb.avg:.3f})\t"
                         "Computation_Percentage_GC: {cp_record_gc.val:.3f}({cp_record_gc.avg:.3f})\t".format(
                            i,
                            len(test_loader),
                            batch_time=batch_time,
                            data_time=data_time,
                            loss=losses,
                            top1=top1,
                            cp_record=cp_record,
                            cp_record_fw=cp_record_fw,
                            cp_record_eb=cp_record_eb,
                            cp_record_gc=cp_record_gc)
            )
            
    if not swa:
        logging.info('Step {} * Prec@1 {top1.avg:.3f}'.format(step, top1=top1))
    else:
        logging.info('Step {} * SWA Prec@1 {top1.avg:.3f}'.format(step, top1=top1))
    
    for layer in range(network_depth):
        print('layer{}_decision'.format(layer + 2))
        for g in range(len(cost_fw)):
            print('{}_ratio{}'.format(g,layerwise_decision_statistics[layer][g].avg))

    return top1.avg


def validate_full_prec(args, test_loader, model, criterion, step):
    batch_time = AverageMeter()
    losses = AverageMeter()
    top1 = AverageMeter()

    bits_full = np.zeros(len(bits))
    grad_bits_full = np.zeros(len(grad_bits))

    # switch to evaluation mode
    model.eval()
    end = time.time()
    for i, (input, target) in enumerate(test_loader):
        target = target.cuda()
        input_var = Variable(input, volatile=True).cuda()
        target_var = Variable(target, volatile=True).cuda()
        
        # compute output
        output, _ = model(input_var, bits_full, grad_bits_full)

        loss = criterion(output, target_var)

        # measure accuracy and record loss
        prec1, = accuracy(output.data, target, topk=(1,))
        top1.update(prec1.item(), input.size(0))
        # skip_ratios.update(skips, input.size(0))
        losses.update(loss.item(), input.size(0))
        batch_time.update(time.time() - end)
        end = time.time()

        if args.gate_type == 'rnn':
            model.module.control.repackage_hidden()
            
    logging.info('Step {} * Full Prec@1 {top1.avg:.3f}, Loss {loss.avg:.3f}'.format(step, top1=top1, loss=losses))

    return top1.avg


def test_model(args):
    global conv_info

    model = models.__dict__[args.arch](args.pretrained, proj_dim=len(bits))
    model = torch.nn.DataParallel(model).cuda()

    if args.resume:
        if os.path.isfile(args.resume):
            logging.info('=> loading checkpoint `{}`'.format(args.resume))
            checkpoint = torch.load(args.resume)
            args.start_iter = checkpoint['iter']
            best_prec1 = checkpoint['best_prec1']
            model.load_state_dict(checkpoint['state_dict'],strict=True)
            logging.info('=> loaded checkpoint `{}` (iter: {})'.format(
                args.resume, checkpoint['iter']
            ))
        else:
            logging.info('=> no checkpoint found at `{}`'.format(args.resume))

    network_depth = sum(model.module.num_layers)

    if conv_info is None:
        conv_info = [1 for _ in range(network_depth)]

    cudnn.benchmark = False
    test_loader = prepare_test_data(dataset=args.dataset,
                                    batch_size=args.batch_size,
                                    shuffle=False,
                                    num_workers=args.workers)
    criterion = nn.CrossEntropyLoss().cuda()
    
    with torch.no_grad():
        validate(args, test_loader, model, criterion, args.start_iter)
        # validate_full_prec(args, test_loader, model, criterion, args.start_iter)


def save_checkpoint(state, is_best, filename='checkpoint.pth.tar'):
    torch.save(state, filename)
    if is_best:
        save_path = os.path.dirname(filename)
        shutil.copyfile(filename, os.path.join(save_path,
                                               'model_best.pth.tar'))


class AverageMeter(object):
    """Computes and stores the average and current value"""

    def __init__(self):
        self.reset()

    def reset(self):
        self.val = 0
        self.avg = 0
        self.sum = 0
        self.count = 0

    def update(self, val, n=1):
        self.val = val
        self.sum += val * n
        self.count += n
        self.avg = self.sum / self.count


class ListAverageMeter(object):
    """Computes and stores the average and current values of a list"""
    def __init__(self):
        self.len = 10000  # set up the maximum length
        self.reset()

    def reset(self):
        self.val = [0] * self.len
        self.avg = [0] * self.len
        self.sum = [0] * self.len
        self.count = 0

    def set_len(self, n):
        self.len = n
        self.reset()

    def update(self, vals, n=1):
        assert len(vals) == self.len, 'length of vals not equal to self.len'
        self.val = vals
        for i in range(self.len):
            self.sum[i] += self.val[i] * n
        self.count += n
        for i in range(self.len):
            self.avg[i] = self.sum[i] / self.count


schedule_cnt = 0
def adjust_target_ratio(args, _iter):
    if args.schedule:
        global schedule_cnt

        assert len(args.target_ratio_schedule) == len(args.schedule) + 1

        if schedule_cnt == 0:
            args.target_ratio = args.target_ratio_schedule[0]
            # if args.weight_bits_schedule is not None:
            #     args.weight_bits = args.weight_bits_schedule[0]
            schedule_cnt += 1

        for step in args.schedule:
            if _iter == step:
                args.target_ratio = args.target_ratio_schedule[schedule_cnt]
                # if args.weight_bits_schedule is not None:
                #     args.weight_bits = args.weight_bits_schedule[schedule_cnt]
                schedule_cnt += 1

        if _iter % args.eval_every == 0:
            logging.info('Iter [{}] target_ratio = {}'.format(_iter, args.target_ratio))


def adjust_learning_rate(args, optimizer, _iter):
    if args.lr_schedule == 'piecewise':
        if args.warm_up and (_iter < 400):
            lr = 0.01
        elif 32000 <= _iter < 48000:
            lr = args.lr * (args.step_ratio ** 1)
        elif _iter >= 48000:
            lr = args.lr * (args.step_ratio ** 2)
        else:
            lr = args.lr

    elif args.lr_schedule == 'linear':
        t = _iter / args.iters
        lr_ratio = 0.01
        if args.warm_up and (_iter < 400):
            lr = 0.01
        elif t < 0.25:
            lr = args.lr
        elif t < 0.75:
            lr = args.lr * (1 - (1-lr_ratio)*(t-0.25)/0.5)
        else:
            lr = args.lr * lr_ratio

    elif args.lr_schedule == 'anneal_cosine':
        lr_min = args.lr * (args.step_ratio ** 2)
        lr_max = args.lr
        lr = lr_min + 1/2 * (lr_max - lr_min) * (1 + np.cos(_iter/args.iters * 3.141592653))

    if _iter % args.eval_every == 0:
        logging.info('Iter [{}] learning rate = {}'.format(_iter, lr))

    for param_group in optimizer.param_groups:
        param_group['lr'] = lr


def accuracy(output, target, topk=(1,)):
    """Computes the precision@k for the specified values of k"""
    maxk = max(topk)
    batch_size = target.size(0)

    _, pred = output.topk(maxk, 1, True, True)
    pred = pred.t()
    correct = pred.eq(target.view(1, -1).expand_as(pred))

    res = []
    for k in topk:
        correct_k = correct[:k].view(-1).float().sum(0)
        res.append(correct_k.mul_(100.0 / batch_size))
    return res


if __name__ == '__main__':
    main()

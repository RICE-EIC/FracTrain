from __future__ import print_function

import torch
import torch.nn as nn
import torch.backends.cudnn as cudnn
from torch.autograd import Variable
import numpy as np

import os
import shutil
import argparse
import time
import logging

import models
from data import *

import util_swa


model_names = sorted(name for name in models.__dict__
                     if name.islower() and not name.startswith('__')
                     and callable(models.__dict__[name])
                     )


def parse_args():
    # hyper-parameters are from ResNet paper
    parser = argparse.ArgumentParser(
        description='PFQ on CIFAR')
    parser.add_argument('--dir', help='annotate the working directory')
    parser.add_argument('--cmd', choices=['train', 'test'], default='train')
    parser.add_argument('--arch', metavar='ARCH', default='cifar10_resnet_38',
                        choices=model_names,
                        help='model architecture: ' +
                             ' | '.join(model_names) +
                             ' (default: cifar10_resnet_38)')
    parser.add_argument('--dataset', '-d', type=str, default='cifar10',
                        choices=['cifar10', 'cifar100'],
                        help='dataset choice')
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
    parser.add_argument('--eval_every', default=400, type=int,
                        help='evaluate model every (default: 1000) iterations')
    parser.add_argument('--num_bits',default=0,type=int,
                        help='num bits for weight and activation')
    parser.add_argument('--num_grad_bits',default=0,type=int,
                        help='num bits for gradient')
    parser.add_argument('--schedule', default=None, type=int, nargs='*',
                        help='precision schedule')
    parser.add_argument('--num_bits_schedule',default=None,type=int,nargs='*',
                        help='schedule for weight/act precision')
    parser.add_argument('--num_grad_bits_schedule',default=None,type=int,nargs='*',
                        help='schedule for grad precision')
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
    parser.add_argument('--momentum_act', default=0.9, type=float,
                        help='momentum for act min/max')
    parser.add_argument('--swa_start', type=float, default=None, help='SWA start step number')
    parser.add_argument('--swa_freq', type=float, default=1170,
                        help='SWA model collection frequency')

    parser.add_argument('--num_turning_point', type=int, default=3)
    parser.add_argument('--initial_threshold', type=float, default=0.15)
    parser.add_argument('--decay', type=float, default=0.4)
    args = parser.parse_args()
    return args

# indicator
class loss_diff_indicator():
    def __init__(self, threshold, decay, epoch_keep=5):
        self.threshold = threshold
        self.decay = decay
        self.epoch_keep = epoch_keep
        self.loss = []
        self.scale_loss = 1
        self.loss_diff = [1 for i in range(1, self.epoch_keep)]

    def reset(self):
        self.loss = []
        self.loss_diff = [1 for i in range(1, self.epoch_keep)]

    def adaptive_threshold(self, turning_point_count):
        decay_1 = self.decay
        decay_2 = self.decay
        if turning_point_count == 1:
            self.threshold *= decay_1
        if turning_point_count == 2:
            self.threshold *= decay_2
        print('threshold decay to {}'.format(self.threshold))

    def get_loss(self, current_epoch_loss):
        if len(self.loss) < self.epoch_keep:
            self.loss.append(current_epoch_loss)
        else:
            self.loss.pop(0)
            self.loss.append(current_epoch_loss)

    def cal_loss_diff(self):
        if len(self.loss) == self.epoch_keep:
            for i in range(len(self.loss)-1):
                loss_now = self.loss[-1]
                loss_pre = self.loss[i]
                self.loss_diff[i] = np.abs(loss_pre - loss_now) / self.scale_loss
            return True
        else:
            return False

    def turning_point_emerge(self):
        flag = self.cal_loss_diff()
        if flag == True:
            print(self.loss_diff)
            for i in range(len(self.loss_diff)):
                if self.loss_diff[i] > self.threshold:
                    return False
            return True
        else:
            return False

def main():
    args = parse_args()
    global save_path
    save_path = args.save_path = os.path.join(args.save_folder, args.arch)
    if not os.path.exists(save_path):
        os.makedirs(save_path)

    models.ACT_FW = args.act_fw
    models.ACT_BW = args.act_bw
    models.GRAD_ACT_ERROR = args.grad_act_error
    models.GRAD_ACT_GC = args.grad_act_gc
    models.WEIGHT_BITS = args.weight_bits
    models.MOMENTUM = args.momentum_act

    args.num_bits = args.num_bits if not (args.act_fw + args.act_bw + args.grad_act_error + args.grad_act_gc + args.weight_bits) else -1

    # config logging file
    args.logger_file = os.path.join(save_path, 'log_{}.txt'.format(args.cmd))
    if os.path.exists(args.logger_file):
        os.remove(args.logger_file)
    handlers = [logging.FileHandler(args.logger_file, mode='w'),
                logging.StreamHandler()]
    logging.basicConfig(level=logging.INFO,
                        datefmt='%m-%d-%y %H:%M',
                        format='%(asctime)s:%(message)s',
                        handlers=handlers)

    global history_score
    history_score = np.zeros((args.iters // args.eval_every, 3))

    # initialize indicator
    # initial_threshold=0.15
    global scale_loss
    scale_loss = 0
    global my_loss_diff_indicator
    my_loss_diff_indicator = loss_diff_indicator(threshold=args.initial_threshold,
                                                 decay=args.decay)

    global turning_point_count
    turning_point_count = 0

    if args.cmd == 'train':
        logging.info('start training {}'.format(args.arch))
        run_training(args)

    elif args.cmd == 'test':
        logging.info('start evaluating {} with checkpoints from {}'.format(
            args.arch, args.resume))
        test_model(args)



def run_training(args):
    # create model
    training_loss = 0
    training_acc = 0

    model = models.__dict__[args.arch](args.pretrained)
    model = torch.nn.DataParallel(model).cuda()

    if args.swa_start is not None:
        print('SWA training')
        swa_model = torch.nn.DataParallel(models.__dict__[args.arch](args.pretrained)).cuda()
        swa_n = 0

    else:
        print('SGD training')

    best_prec1 = 0
    best_iter = 0

    best_swa_prec = 0
    best_swa_iter = 0

    # best_full_prec = 0

    if args.resume:
        if os.path.isfile(args.resume):
            logging.info('=> loading checkpoint `{}`'.format(args.resume))
            checkpoint = torch.load(args.resume)
            args.start_iter = checkpoint['iter']
            best_prec1 = checkpoint['best_prec1']
            model.load_state_dict(checkpoint['state_dict'])

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

            logging.info('=> loaded checkpoint `{}` (iter: {})'.format(
                args.resume, checkpoint['iter']
            ))
        else:
            logging.info('=> no checkpoint found at `{}`'.format(args.resume))

    cudnn.benchmark = False

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

    # define loss function (criterion) and optimizer
    criterion = nn.CrossEntropyLoss().cuda()

    optimizer = torch.optim.SGD(model.parameters(), args.lr,
                                momentum=args.momentum,
                                weight_decay=args.weight_decay)

    # optimizer = torch.optim.Adam(model.parameters(), args.lr,
    #                             weight_decay=args.weight_decay)

    batch_time = AverageMeter()
    data_time = AverageMeter()
    losses = AverageMeter()
    top1 = AverageMeter()
    cr = AverageMeter()

    end = time.time()

    global scale_loss
    global turning_point_count
    global my_loss_diff_indicator

    i = args.start_iter
    while i < args.iters:
        for input, target in train_loader:
            # measuring data loading time
            data_time.update(time.time() - end)

            model.train()
            adjust_learning_rate(args, optimizer, i)
            # adjust_precision(args, i)
            adaptive_adjust_precision(args, turning_point_count)

            i += 1

            fw_cost = args.num_bits*args.num_bits/32/32
            eb_cost = args.num_bits*args.num_grad_bits/32/32
            gc_cost = eb_cost
            cr.update((fw_cost+eb_cost+gc_cost)/3)

            target = target.squeeze().long().cuda()
            input_var = Variable(input).cuda()
            target_var = Variable(target).cuda()

            # compute output
            output = model(input_var, args.num_bits, args.num_grad_bits)
            loss = criterion(output, target_var)
            training_loss += loss.item()

            # measure accuracy and record loss
            prec1, = accuracy(output.data, target, topk=(1,))
            losses.update(loss.item(), input.size(0))
            top1.update(prec1.item(), input.size(0))
            training_acc += prec1.item()

            # compute gradient and do SGD step
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()

            # measure elapsed time
            batch_time.update(time.time() - end)
            end = time.time()

            # print log
            if i % args.print_freq == 0:
                logging.info("Iter: [{0}/{1}]\t"
                             "Time {batch_time.val:.3f} ({batch_time.avg:.3f})\t"
                             "Data {data_time.val:.3f} ({data_time.avg:.3f})\t"
                             "Loss {loss.val:.3f} ({loss.avg:.3f})\t"
                             "Prec@1 {top1.val:.3f} ({top1.avg:.3f})\t".format(
                                i,
                                args.iters,
                                batch_time=batch_time,
                                data_time=data_time,
                                loss=losses,
                                top1=top1)
                )


            if args.swa_start is not None and i >= args.swa_start and i % args.swa_freq == 0:
                util_swa.moving_average(swa_model, model, 1.0 / (swa_n + 1))
                swa_n += 1
                util_swa.bn_update(swa_loader, swa_model, args.num_bits, args.num_grad_bits)
                prec1 = validate(args, test_loader, swa_model, criterion, i, swa=True)

                if prec1 > best_swa_prec:
                    best_swa_prec = prec1
                    best_swa_iter = i

                # print("Current Best SWA Prec@1: ", best_swa_prec)
                # print("Current Best SWA Iteration: ", best_swa_iter)

            if (i % args.eval_every == 0 and i > 0) or (i == args.iters):
                # record training loss and test accuracy
                global history_score
                epoch = i // args.eval_every
                epoch_loss = training_loss / len(train_loader)
                with torch.no_grad():
                    prec1 = validate(args, test_loader, model, criterion, i)
                    # prec_full = validate_full_prec(args, test_loader, model, criterion, i)
                history_score[epoch-1][0] = epoch_loss
                history_score[epoch-1][1] = np.round(training_acc / len(train_loader), 2)
                history_score[epoch-1][2] = prec1
                training_loss = 0
                training_acc = 0

                np.savetxt(os.path.join(save_path, 'record.txt'), history_score, fmt = '%10.5f', delimiter=',')

                # apply indicator
                # if epoch == 1:
                #     logging.info('initial loss value: {}'.format(epoch_loss))
                #     my_loss_diff_indicator.scale_loss = epoch_loss
                if epoch <= 10:
                    scale_loss += epoch_loss
                    logging.info('scale_loss at epoch {}: {}'.format(epoch, scale_loss / epoch))
                    my_loss_diff_indicator.scale_loss = scale_loss / epoch
                if turning_point_count < args.num_turning_point:
                    my_loss_diff_indicator.get_loss(epoch_loss)
                    flag = my_loss_diff_indicator.turning_point_emerge()
                    if flag == True:
                        turning_point_count += 1
                        logging.info('find {}-th turning point at {}-th epoch'.format(turning_point_count, epoch))
                        # print('find {}-th turning point at {}-th epoch'.format(turning_point_count, epoch))
                        my_loss_diff_indicator.adaptive_threshold(turning_point_count=turning_point_count)
                        my_loss_diff_indicator.reset()

                logging.info('Epoch [{}] num_bits = {} num_grad_bits = {}'.format(epoch, args.num_bits, args.num_grad_bits))

                # print statistics
                is_best = prec1 > best_prec1
                if is_best:
                    best_prec1 = prec1
                    best_iter = i
                # best_full_prec = max(prec_full, best_full_prec)

                logging.info("Current Best Prec@1: {}".format(best_prec1))
                logging.info("Current Best Iteration: {}".format(best_iter))
                logging.info("Current Best SWA Prec@1: {}".format(best_swa_prec))
                logging.info("Current Best SWA Iteration: {}".format(best_swa_iter))
                # print("Current Best Full Prec@1: ", best_full_prec)

                # checkpoint_path = os.path.join(args.save_path, 'checkpoint_{:05d}_{:.2f}.pth.tar'.format(i, prec1))
                checkpoint_path = os.path.join(args.save_path, 'ckpt.pth.tar')
                save_checkpoint({
                    'iter': i,
                    'arch': args.arch,
                    'state_dict': model.state_dict(),
                    'best_prec1': best_prec1,
                    'swa_state_dict' : swa_model.state_dict() if args.swa_start is not None else None,
                    'swa_n' : swa_n if args.swa_start is not None else None,
                    'best_swa_prec' : best_swa_prec if args.swa_start is not None else None,
                },
                    is_best, filename=checkpoint_path)
                # shutil.copyfile(checkpoint_path, os.path.join(args.save_path,
                                                              # 'checkpoint_latest'
                                                              # '.pth.tar'))

                if i == args.iters:
                    print("Best accuracy: "+str(best_prec1))
                    history_score[-1][0] = best_prec1
                    np.savetxt(os.path.join(save_path, 'record.txt'), history_score, fmt = '%10.5f', delimiter=',')
                    break


def validate(args, test_loader, model, criterion, step, swa=False):
    batch_time = AverageMeter()
    losses = AverageMeter()
    top1 = AverageMeter()

    # switch to evaluation mode
    model.eval()
    end = time.time()
    for i, (input, target) in enumerate(test_loader):
        target = target.squeeze().long().cuda()
        input_var = Variable(input, volatile=True).cuda()
        target_var = Variable(target, volatile=True).cuda()

        # compute output
        output = model(input_var, args.num_bits, args.num_grad_bits)
        loss = criterion(output, target_var)

        # measure accuracy and record loss
        prec1, = accuracy(output.data, target, topk=(1,))
        top1.update(prec1.item(), input.size(0))
        losses.update(loss.item(), input.size(0))
        batch_time.update(time.time() - end)
        end = time.time()

        if (i % args.print_freq == 0) or (i == len(test_loader) - 1):
            logging.info(
                'Test: [{}/{}]\t'
                'Time: {batch_time.val:.4f}({batch_time.avg:.4f})\t'
                'Loss: {loss.val:.3f}({loss.avg:.3f})\t'
                'Prec@1: {top1.val:.3f}({top1.avg:.3f})\t'.format(
                    i, len(test_loader), batch_time=batch_time,
                    loss=losses, top1=top1
                )
            )

    if not swa:
        logging.info('Step {} * Prec@1 {top1.avg:.3f}'.format(step, top1=top1))
    else:
        logging.info('Step {} * SWA Prec@1 {top1.avg:.3f}'.format(step, top1=top1))

    return top1.avg


def validate_full_prec(args, test_loader, model, criterion, step):
    batch_time = AverageMeter()
    losses = AverageMeter()
    top1 = AverageMeter()

    # switch to evaluation mode
    model.eval()
    end = time.time()
    for i, (input, target) in enumerate(test_loader):
        target = target.squeeze().long().cuda()
        input_var = Variable(input, volatile=True).cuda()
        target_var = Variable(target, volatile=True).cuda()

        # compute output
        output = model(input_var, 0, 0)
        loss = criterion(output, target_var)

        # measure accuracy and record loss
        prec1, = accuracy(output.data, target, topk=(1,))
        top1.update(prec1.item(), input.size(0))
        losses.update(loss.item(), input.size(0))
        batch_time.update(time.time() - end)
        end = time.time()


    logging.info('Step {} * Full Prec@1 {top1.avg:.3f}'.format(step, top1=top1))
    return top1.avg


def test_model(args):
    # create model
    model = models.__dict__[args.arch](args.pretrained)
    model = torch.nn.DataParallel(model).cuda()

    if args.resume:
        if os.path.isfile(args.resume):
            logging.info('=> loading checkpoint `{}`'.format(args.resume))
            checkpoint = torch.load(args.resume)
            args.start_iter = checkpoint['iter']
            best_prec1 = checkpoint['best_prec1']
            model.load_state_dict(checkpoint['state_dict'])
            logging.info('=> loaded checkpoint `{}` (iter: {})'.format(
                args.resume, checkpoint['iter']
            ))
        else:
            logging.info('=> no checkpoint found at `{}`'.format(args.resume))

    cudnn.benchmark = False
    test_loader = prepare_test_data(dataset=args.dataset,
                                    batch_size=args.batch_size,
                                    shuffle=False,
                                    num_workers=args.workers)
    criterion = nn.CrossEntropyLoss().cuda()

    # validate(args, test_loader, model, criterion)

    with torch.no_grad():
        prec1 = validate(args, test_loader, model, criterion, args.start_iter)
        prec_full = validate_full_prec(args, test_loader, model, criterion, args.start_iter)


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


schedule_cnt = 0
def adjust_precision(args, _iter):
    if args.schedule:
        global schedule_cnt

        assert len(args.num_bits_schedule) == len(args.schedule) + 1
        assert len(args.num_grad_bits_schedule) == len(args.schedule) + 1

        if schedule_cnt == 0:
            args.num_bits = args.num_bits_schedule[0]
            args.num_grad_bits = args.num_grad_bits_schedule[0]
            schedule_cnt += 1

        for step in args.schedule:
            if _iter == step:
                args.num_bits = args.num_bits_schedule[schedule_cnt]
                args.num_grad_bits = args.num_grad_bits_schedule[schedule_cnt]
                schedule_cnt += 1

        if _iter % args.eval_every == 0:
            logging.info('Iter [{}] num_bits = {} num_grad_bits = {}'.format(_iter, args.num_bits, args.num_grad_bits))

def adaptive_adjust_precision(args, turning_point_count):
    args.num_bits = args.num_bits_schedule[turning_point_count]
    args.num_grad_bits = args.num_grad_bits_schedule[turning_point_count]


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
        elif t < 0.5:
            lr = args.lr
        elif t < 0.9:
            lr = args.lr * (1 - (1-lr_ratio)*(t-0.5)/0.4)
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

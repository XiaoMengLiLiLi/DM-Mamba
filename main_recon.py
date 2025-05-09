#!/usr/bin/env python
# -*- coding: utf-8 -*-
# Python version: 3.6
import warnings
warnings.simplefilter("ignore", UserWarning)

import torch
import os
from utils.options import args_parser
from models.recon_Update import train_recon
from models.evaluation import evaluate_recon
from data.mri_data import SliceData, DataTransform
from data.subsample import create_mask_for_mask_type
from tensorboardX import SummaryWriter
import pathlib
from torch.utils.data import DataLoader

from MambaRecon.FreMamba import FreMamba
from thop import profile, clever_format

def main():
    os.environ["HDF5_USE_FILE_LOCKING"] = 'FALSE'
    torch.backends.cudnn.enabled = True
    torch.backends.cudnn.benchmark = True
    # parse args
    args, parser = args_parser()
    path_dict = {'F': pathlib.Path(args.F_path)}
    resolution_dict = {'F': 256}
    rate_dict = {'F': 1.0}
    args.device = torch.device('cuda:{}'.format(args.gpu[0]) if torch.cuda.is_available() and args.gpu != -1 else 'cpu')
    args.resolution = resolution_dict[args.train_dataset]
    if not os.path.exists(args.save_dir):
        os.mkdir(args.save_dir)

    writer = SummaryWriter(log_dir=args.save_dir/ 'summary')
    print_options(args, parser)
    def save_networks(net, epoch, local=False, local_no = None):
        """Save all the networks to the disk.        Parameters:
            epoch (int) -- current epoch; used in the file name '%s_net_%s.pth' % (epoch, name)
        """
        if local:
            save_filename = '%s_C%s_net.pth' % (epoch,local_no)
        else:
            save_filename = '%s_net.pth' % (epoch)
        save_path = os.path.join(args.save_dir, save_filename)
        if len(args.gpu) > 1 and torch.cuda.is_available():
            torch.save(net.module.cpu().state_dict(), save_path)
            net.to(args.device)
        else:
            torch.save(net.cpu().state_dict(), save_path)
            net.to(args.device)

    # data loader------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------
    def _create_dataset(data_path,data_transform, data_partition, sequence, bs, shuffle, sample_rate=None, display=False):
        sample_rate = sample_rate or args.sample_rate
        dataset = SliceData(
            root=data_path / data_partition,
            transform=data_transform,
            sample_rate=sample_rate,
            challenge=args.challenge,
            sequence=sequence
        )
        if display:
            dataset = [dataset[i] for i in range(100,108)]
        return DataLoader(dataset, batch_size=bs, shuffle=shuffle, pin_memory=True,num_workers=16)

    if args.challenge == 'singlecoil':
        mask = create_mask_for_mask_type(args.mask_type, args.center_fractions,
                                         args.accelerations)
        train_data_transform = DataTransform(args.resolution, args.challenge, mask, use_seed=False)
        val_data_transform = DataTransform(args.resolution, args.challenge, mask, use_seed=True)

        if args.phase == 'train':
            dataset_train = _create_dataset(path_dict[args.train_dataset]/args.sequence,train_data_transform, 'train', args.sequence, args.bs, True, rate_dict[args.train_dataset])
            dataset_val = _create_dataset(path_dict[args.test_dataset]/args.sequence,val_data_transform, 'val', args.sequence, 1, False, 1.0)
    else:
        exit('Error: unrecognized challenge')

    # build model-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------
    net = FreMamba(
        img_size=args.img_size,
        patch_size=args.patch_size,
        in_chans=args.in_chans,
        embed_dim=args.embed_dim,
        depths=args.depths,
        drop_rate=args.drop_rate,
        d_state=args.d_state,
        mlp_ratio=args.mlp_ratio,
        drop_path_rate=args.drop_path_rate,
        norm_layer=args.norm_layer,
        patch_norm=args.patch_norm,
        use_checkpoint=args.use_checkpoint,
        img_range=args.img_range,
        resi_connection=args.resi_connection,
        ).to(args.device)
    # net = ExpoMamba(n_channels=2, bilinear=True, base_channels=48).to(args.device)
    if len(args.gpu) > 1:
        net = torch.nn.DataParallel(net, args.gpu)

    # FLOPs & Params------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------
    input_shape = (2, 256, 256)
    input_tensor = torch.randn(1, *input_shape).to(args.device)
    flops, params = profile(net, inputs=(input_tensor,))
    flops, params = clever_format([flops, params], "%.3f")
    print("FLOPs: %s" % (flops))
    print("params: %s" % (params))

    # training-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------
    if args.phase == 'train':
        start_epoch = -1
        if args.continues:
            if len(args.gpu) > 1:
                net.module.load_state_dict(torch.load(args.checkpoint))
            else:
                net.load_state_dict(torch.load(args.checkpoint))
            print('Load checkpoint :', args.checkpoint)
            start_epoch = int(args.checkpoint.split('/')[-1].split('_')[0])

        optimizer = torch.optim.Adam(net.parameters(), lr=args.lr)
        scheduler = torch.optim.lr_scheduler.StepLR(optimizer, step_size=args.lr_step_size, gamma=args.lr_gamma)

        for iter in range(start_epoch+1,args.epochs):
            loss_avg = train_recon(net, dataset_train, optimizer, iter, args, writer)
            scheduler.step(iter)
            torch.cuda.empty_cache()
            print('Round {:3d}, Average loss {:.3f}'.format(iter, loss_avg))
            print('saving the model at the end of epoch %d' % (iter))
            save_networks(net, iter)
            print('Evaluation ...')
            evaluate_recon(net, dataset_val, args, writer, iter)
            torch.cuda.empty_cache()
        writer.close()


def print_networks(net):
    """Print the total number of parameters in the network and (if verbose) network architecture

    Parameters:
        verbose (bool) -- if verbose: print the network architecture
    """
    print('---------- Networks initialized -------------')
    num_params = 0
    for param in net.parameters():
        num_params += param.numel()
    print(net)
    print('[Network] Total number of parameters : %.3f M' % (num_params / 1e6))
    print('-----------------------------------------------')

def print_options(opt,parser):
        """Print and save options

        It will print both current options and default values(if different).
        It will save options into a text file / [checkpoints_dir] / opt.txt
        """
        message = ''
        message += '----------------- Options ---------------\n'
        for k, v in sorted(vars(opt).items()):
            comment = ''
            default = parser.get_default(k)
            if v != default:
                comment = '\t[default: %s]' % str(default)
            message += '{:>25}: {:<30}{}\n'.format(str(k), str(v), comment)
        message += '----------------- End -------------------'
        print(message)

        # save to the disk
        file_name = os.path.join(opt.save_dir, '{}_opt.txt'.format(opt.phase))
        with open(file_name, 'wt') as opt_file:
            opt_file.write(message)
            opt_file.write('\n')
if __name__ == '__main__':
    main()


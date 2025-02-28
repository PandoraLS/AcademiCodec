# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.
"""Command-line for audio compression."""
import argparse
import os
import sys
import typing as tp
from collections import OrderedDict
from pathlib import Path

import torch
import torchaudio
from academicodec.models.encodec.net3 import SoundStream


def save_audio(wav: torch.Tensor,
               path: tp.Union[Path, str],
               sample_rate: int,
               rescale: bool=False):
    limit = 0.99
    mx = wav.abs().max()
    if rescale:
        wav = wav * min(limit / mx, 1)
    else:
        wav = wav.clamp(-limit, limit)
    torchaudio.save(
        path,
        wav,
        sample_rate=sample_rate,
        encoding='PCM_S',
        bits_per_sample=16)


def convert_audio(wav: torch.Tensor,
                  sr: int,
                  target_sr: int,
                  target_channels: int):
    assert wav.shape[0] in [1, 2], "Audio must be mono or stereo."
    if target_channels == 1:
        wav = wav.mean(0, keepdim=True)
    elif target_channels == 2:
        *shape, _, length = wav.shape
        wav = wav.expand(*shape, target_channels, length)
    elif wav.shape[0] == 1:
        wav = wav.expand(target_channels, -1)
    wav = torchaudio.transforms.Resample(sr, target_sr)(wav)
    return wav


def get_parser():
    parser = argparse.ArgumentParser(
        'encodec',
        description='High fidelity neural audio codec. '
        'If input is a .ecdc, decompresses it. '
        'If input is .wav, compresses it. If output is also wav, '
        'do a compression/decompression cycle.')
    parser.add_argument(
        '--input',
        type=Path,
        help='Input file, whatever is supported by torchaudio on your system.')
    parser.add_argument(
        '--output',
        type=Path,
        nargs='?',
        help='Output file, otherwise inferred from input file.')
    parser.add_argument(
        '--resume_path', type=str, default='resume_path', help='resume_path')
    parser.add_argument(
        '--sr', type=int, default=16000, help='sample rate of model')
    parser.add_argument(
        '-r',
        '--rescale',
        action='store_true',
        help='Automatically rescale the output to avoid clipping.')
    parser.add_argument(
        '--ratios',
        type=int,
        nargs='+',
        # probs(ratios) = hop_size
        default=[8, 5, 4, 2],
        help='ratios of SoundStream, shoud be set for different hop_size (32d, 320, 240d, ...)'
    )
    parser.add_argument(
        '--target_bandwidths',
        type=float,
        nargs='+',
        # default for 16k_320d
        default=[1, 1.5, 2, 4, 6, 12],
        help='target_bandwidths of net3.py')

    return parser


def fatal(*args):
    print(*args, file=sys.stderr)
    sys.exit(1)


# 这只是打印了但是没有真的 clip
def check_clipping(wav, rescale):
    if rescale:
        return
    mx = wav.abs().max()
    limit = 0.99
    if mx > limit:
        print(
            f"Clipping!! max scale {mx}, limit is {limit}. "
            "To avoid clipping, use the `-r` option to rescale the output.",
            file=sys.stderr)


def test_one(args, wav_root, store_root, rescale, soundstream):
    # torchaudio.load 的采样率为原始音频的采样率，不会自动下采样
    wav, sr = torchaudio.load(wav_root)
    # 取单声道, output shape [1, T]
    wav = wav[0].unsqueeze(0)
    # 重采样为模型的采样率
    wav = torchaudio.transforms.Resample(orig_freq=sr, new_freq=args.sr)(wav)
    # add batch axis
    wav = wav.unsqueeze(1).cuda()
    # compressing
    compressed = soundstream.encode(wav, target_bw=12)
    print('finish compressing')
    out = soundstream.decode(compressed)
    out = out.detach().cpu().squeeze(0)
    check_clipping(out, rescale)
    save_audio(wav=out, path=store_root, sample_rate=args.sr, rescale=rescale)
    print('finish decompressing')


def remove_encodec_weight_norm(model):
    from academicodec.modules import SConv1d
    from academicodec.modules.seanet import SConvTranspose1d
    from academicodec.modules.seanet import SEANetResnetBlock
    from torch.nn.utils import remove_weight_norm

    encoder = model.encoder.model
    for key in encoder._modules:
        if isinstance(encoder._modules[key], SEANetResnetBlock):
            remove_weight_norm(encoder._modules[key].shortcut.conv.conv)
            block_modules = encoder._modules[key].block._modules
            for skey in block_modules:
                if isinstance(block_modules[skey], SConv1d):
                    remove_weight_norm(block_modules[skey].conv.conv)
        elif isinstance(encoder._modules[key], SConv1d):
            remove_weight_norm(encoder._modules[key].conv.conv)

    decoder = model.decoder.model
    for key in decoder._modules:
        if isinstance(decoder._modules[key], SEANetResnetBlock):
            remove_weight_norm(decoder._modules[key].shortcut.conv.conv)
            block_modules = decoder._modules[key].block._modules
            for skey in block_modules:
                if isinstance(block_modules[skey], SConv1d):
                    remove_weight_norm(block_modules[skey].conv.conv)
        elif isinstance(decoder._modules[key], SConvTranspose1d):
            remove_weight_norm(decoder._modules[key].convtr.convtr)
        elif isinstance(decoder._modules[key], SConv1d):
            remove_weight_norm(decoder._modules[key].conv.conv)


def test_batch():
    args = get_parser().parse_args()
    print("args.target_bandwidths:", args.target_bandwidths)
    if not args.input.exists():
        fatal(f"Input file {args.input} does not exist.")
    input_lists = os.listdir(args.input)
    input_lists.sort()
    soundstream = SoundStream(
        n_filters=32,
        D=512,
        ratios=args.ratios,
        sample_rate=args.sr,
        target_bandwidths=args.target_bandwidths)
    parameter_dict = torch.load(args.resume_path)
    new_state_dict = OrderedDict()
    # k 为 module.xxx.weight, v 为权重
    for k, v in parameter_dict.items():
        # 截取`module.`后面的xxx.weight
        name = k[7:]
        new_state_dict[name] = v
    soundstream.load_state_dict(new_state_dict)  # load model
    remove_encodec_weight_norm(soundstream)
    soundstream = soundstream.cuda()
    os.makedirs(args.output, exist_ok=True)
    for audio in input_lists:
        test_one(
            args=args,
            wav_root=os.path.join(args.input, audio),
            store_root=os.path.join(args.output, audio),
            rescale=args.rescale,
            soundstream=soundstream)


if __name__ == '__main__':
    test_batch()

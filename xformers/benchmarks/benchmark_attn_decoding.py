# Copyright (c) Facebook, Inc. and its affiliates. All rights reserved.
#
# This source code is licensed under the BSD license found in the
# LICENSE file in the root directory of this source tree.


from typing import Any

import torch
from torch.utils import benchmark
from utils import benchmark_main_helper2

import xformers.ops as xops

min_run_time = 0.5
device = torch.device("cuda")


CASES = [
    dict(B=max(1, 2 ** (16 - i)), Mq=1, Mkv=2**i, Hq=16, Hkv=1, K=128)
    for i in range(8, 18)
] + [
    dict(B=max(1, 2 ** (16 - i)), Mq=1, Mkv=2**i, Hq=16, Hkv=2, K=128)
    for i in range(8, 18)
]
# CASES = [
#     dict(B=i, Mq=1, Mkv=j, Hq=16, Hkv=1, K=128)
#     for i in [1, 2, 16, 32]
#     for j in [714, 715, 716, 914, 915, 916, 1314, 1315, 1316, 1714, 1715, 1716]
# ] 
# + [
#     dict(B=i, Mq=1, Mkv=j, Hq=16, Hkv=2, K=128)
#     for i in [1, 2, 16, 32]
#     for j in [74, 75, 76, 94, 95, 96]
# ]
# CASES = [
#     dict(B=i, Mq=1, Mkv=j, Hq=16, Hkv=1, K=128)
#     for i in [1, 2, 16, 32, 64, 128]
#     for j in [64, 12, 256, 512, 1024, 2048, 4096, 8192, 16384, 32768]
# ] + [
#     dict(B=i, Mq=1, Mkv=j, Hq=16, Hkv=2, K=128)
#     for i in [1, 2, 16, 32, 64, 128]
#     for j in [64, 12, 256, 512, 1024, 2048, 4096, 8192, 16384, 32768]
# ]


def _setup_test(
    functions, fw: bool = False, bw: bool = False, cuda_graph: bool = True, **kwargs
):
    for k, benchmark_cls in functions.items():
        benchmark_object = benchmark_cls(**kwargs, bw=bw)
        label = benchmark_object.label
        label += "fw" if fw else ""
        label += "bw" if bw else ""

        def run_one():
            if fw:
                benchmark_object.fw()
            if bw:
                benchmark_object.bw()

        if cuda_graph:
            run_one()
            g = torch.cuda.CUDAGraph()
            with torch.cuda.graph(g):
                run_one()

            def run_one():
                g.replay()

        yield benchmark.Timer(
            stmt="fn()",
            globals={
                "fn": run_one,
            },
            label=label,
            description=k,
            sub_label=benchmark_object.sub_label,
        )


class AttentionDecodingFlashDecoding:
    OP: Any = xops.fmha.flash.FwOp

    def __init__(
        self, B: int, Mq: int, Mkv: int, Hq: int, Hkv: int, K: int, bw: bool
    ) -> None:
        dtype = torch.float16
        self.sub_label = f"B={B} Mq={Mq} Mkv={Mkv} Hq={Hq} Hkv={Hkv} K={K}"
        self.label = "attn_decoding"
        self.shapes = (B, Mq, Mkv, Hq, Hkv, K)

        assert Hkv <= Hq
        assert Hq % Hkv == 0
        self.q = torch.randn(
            [B, Mq, Hkv, Hq // Hkv, K], device="cuda", dtype=dtype, requires_grad=bw
        )
        self.k = torch.randn(
            [B, Mkv, Hkv, 1, K], device="cuda", dtype=dtype, requires_grad=bw
        ).expand(-1, -1, -1, Hq // Hkv, -1)
        self.v = torch.randn(
            [B, Mkv, Hkv, 1, K], device="cuda", dtype=dtype, requires_grad=bw
        ).expand(-1, -1, -1, Hq // Hkv, -1)

        if Hq == Hkv:
            self.q = self.q[:, :, :, 0]
            self.k = self.k[:, :, :, 0]
            self.v = self.v[:, :, :, 0]
        if Hkv == 1:
            self.q = self.q[:, :, 0]
            self.k = self.k[:, :, 0]
            self.v = self.v[:, :, 0]

    def fw(self) -> None:
        xops.memory_efficient_attention_forward(self.q, self.k, self.v, op=self.OP)


class AttentionDecodingSplitKV(AttentionDecodingFlashDecoding):
    OP = xops.fmha.triton_splitk.FwOp


class AttentionDecodingSplitKVS1(AttentionDecodingFlashDecoding):
    OP = xops.fmha.triton_splitk.FwOp_S1


class AttentionDecodingSplitKVS2(AttentionDecodingFlashDecoding):
    OP = xops.fmha.triton_splitk.FwOp_S2


class AttentionDecodingSplitKVS4(AttentionDecodingFlashDecoding):
    OP = xops.fmha.triton_splitk.FwOp_S4


class AttentionDecodingSplitKVS8(AttentionDecodingFlashDecoding):
    OP = xops.fmha.triton_splitk.FwOp_S8


class AttentionDecodingSplitKVS16(AttentionDecodingFlashDecoding):
    OP = xops.fmha.triton_splitk.FwOp_S16


class AttentionDecodingSplitKVS32(AttentionDecodingFlashDecoding):
    OP = xops.fmha.triton_splitk.FwOp_S32


class AttentionDecodingSplitKVS64(AttentionDecodingFlashDecoding):
    OP = xops.fmha.triton_splitk.FwOp_S64


class AttentionDecodingSplitKVS128(AttentionDecodingFlashDecoding):
    OP = xops.fmha.triton_splitk.FwOp_S128


class AttentionDecodingPyTorchRepeat(AttentionDecodingFlashDecoding):
    def fw(self) -> None:
        B, Mq, Mkv, Hq, Hkv, K = self.shapes
        scale = 1 / K**0.5
        q = self.q.reshape([B, Mq, -1, K]).permute(0, 2, 1, 3)
        k = self.k.reshape([B, Mkv, -1, K]).permute(0, 2, 1, 3)
        v = self.v.reshape([B, Mkv, -1, K]).permute(0, 2, 1, 3)
        attn = (q @ k.transpose(-1, -2)).softmax(-1) * scale
        return attn @ v


BENCHMARKS = {
    "pytorch": AttentionDecodingPyTorchRepeat,
    # "flash-decoding": AttentionDecodingFlashDecoding,
    "flash-attention": AttentionDecodingFlashDecoding,
    "triton_splitK": AttentionDecodingSplitKV,
    # "triton_splitK_S1": AttentionDecodingSplitKVS1,
    # "triton_splitK_S2": AttentionDecodingSplitKVS2,
    # "triton_splitK_S4": AttentionDecodingSplitKVS4,
    # "triton_splitK_S8": AttentionDecodingSplitKVS8,
    # "triton_splitK_S16": AttentionDecodingSplitKVS16,
    # "triton_splitK_S32": AttentionDecodingSplitKVS32,
    # "triton_splitK_S64": AttentionDecodingSplitKVS64,
    # "triton_splitK_S128": AttentionDecodingSplitKVS128,
}


# try:
#     import flash_attn

#     class AttentionDecodingFlashAttention(AttentionDecodingFlashDecoding):
#         def fw(self) -> None:
#             q, k, v = self.q, self.k, self.v
#             if q.ndim == 5:
#                 B, Mq, H1, H2, K = q.shape
#                 B, Mkv, H1, H2, K = k.shape
#                 q = q.reshape([B, Mq, H1 * H2, K])
#                 k = k[:, :, :, 0]
#                 v = v[:, :, :, 0]
#             return flash_attn.flash_attn_func(q, k, v)

#     BENCHMARKS[
#         f"flash-attention@{flash_attn.__version__}"
#     ] = AttentionDecodingFlashAttention
# except ImportError:
#     pass


benchmark_main_helper2(
    "attn_decoding",
    fw=True,
    cases=CASES,
    functions=BENCHMARKS,
    min_run_time=min_run_time,
)

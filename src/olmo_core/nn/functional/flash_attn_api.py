from typing import Optional

import torch
import torch.distributed as dist

from olmo_core.distributed.parallel.context_parallel import (
    ContextParallelLoadBalancerType,
)

try:
    import flash_attn  # type: ignore
except ImportError:
    flash_attn = None

try:
    import ring_flash_attn  # type: ignore
except ImportError:
    ring_flash_attn = None


def _flatten_batch_dim(x: torch.Tensor) -> torch.Tensor:
    B, T, *other = x.shape
    return x.view(B * T, *other)


def dispatch_flash_attn(
    q: torch.Tensor,
    k: torch.Tensor,
    v: torch.Tensor,
    *,
    cu_seqlens: Optional[torch.Tensor] = None,
    cu_seqlens_q: Optional[torch.Tensor] = None,
    cu_seqlens_k: Optional[torch.Tensor] = None,
    max_seqlen: Optional[int] = None,
    max_seqlen_q: Optional[int] = None,
    max_seqlen_k: Optional[int] = None,
    dropout_p: float = 0.0,
    softmax_scale: Optional[float] = None,
    causal: bool = False,
) -> torch.Tensor:
    if flash_attn is None:
        raise RuntimeError("flash-attn is required!")

    if cu_seqlens is not None:
        if cu_seqlens_q is None:
            cu_seqlens_q = cu_seqlens
        if cu_seqlens_k is None:
            cu_seqlens_k = cu_seqlens
    if max_seqlen is not None:
        if max_seqlen_q is None:
            max_seqlen_q = max_seqlen
        if max_seqlen_k is None:
            max_seqlen_k = max_seqlen

    varlen = all(x is not None for x in (cu_seqlens_q, cu_seqlens_k, max_seqlen_q, max_seqlen_k))

    if varlen:
        return flash_attn.flash_attn_varlen_func(
            _flatten_batch_dim(q),
            _flatten_batch_dim(k),
            _flatten_batch_dim(v),
            cu_seqlens_q,
            cu_seqlens_k,
            max_seqlen_q,
            max_seqlen_k,
            dropout_p=dropout_p,
            softmax_scale=softmax_scale,
            causal=causal,
        )
    else:
        return flash_attn.flash_attn_func(
            q,
            k,
            v,
            dropout_p=dropout_p,
            softmax_scale=softmax_scale,
            causal=causal,
        )


def dispatch_flash_attn_qkvpacked(
    qkv: torch.Tensor,
    *,
    cu_seqlens: Optional[torch.Tensor] = None,
    max_seqlen: Optional[int],
    dropout_p: float = 0.0,
    softmax_scale: Optional[float] = None,
    causal: bool = False,
) -> torch.Tensor:
    if flash_attn is None:
        raise RuntimeError("flash-attn is required!")

    if cu_seqlens is not None and max_seqlen is not None:
        return flash_attn.flash_attn_varlen_qkvpacked_func(
            _flatten_batch_dim(qkv),
            cu_seqlens,
            max_seqlen,
            dropout_p=dropout_p,
            softmax_scale=softmax_scale,
            causal=causal,
        )
    else:
        return flash_attn.flash_attn_qkvpacked_func(
            qkv, dropout_p=dropout_p, softmax_scale=softmax_scale, causal=causal
        )


@torch._dynamo.disable()
def dispatch_ring_flash_attn(
    q: torch.Tensor,
    k: torch.Tensor,
    v: torch.Tensor,
    *,
    group: dist.ProcessGroup,
    strategy: ContextParallelLoadBalancerType,
    cu_seqlens: Optional[torch.Tensor] = None,
    cu_seqlens_q: Optional[torch.Tensor] = None,
    cu_seqlens_k: Optional[torch.Tensor] = None,
    max_seqlen: Optional[int] = None,
    max_seqlen_q: Optional[int] = None,
    max_seqlen_k: Optional[int] = None,
    dropout_p: float = 0.0,
    softmax_scale: Optional[float] = None,
    causal: bool = False,
) -> torch.Tensor:
    if ring_flash_attn is None:
        raise RuntimeError("flash-attn and ring-flash-attn are required!")

    if strategy == ContextParallelLoadBalancerType.zig_zag:
        if any(x is not None for x in (cu_seqlens_q, cu_seqlens_k, max_seqlen_q, max_seqlen_k)):
            raise RuntimeError(
                f"{strategy} load balancing strategy requires unified QK doc lengths"
            )

        if cu_seqlens is not None and max_seqlen is not None:
            out = ring_flash_attn.zigzag_ring_flash_attn_varlen_func(
                _flatten_batch_dim(q),
                _flatten_batch_dim(k),
                _flatten_batch_dim(v),
                cu_seqlens,
                max_seqlen,
                dropout_p=dropout_p,
                causal=causal,
                softmax_scale=softmax_scale,
                group=group,
            )
        else:
            out = ring_flash_attn.zigzag_ring_flash_attn_func(
                q,
                k,
                v,
                dropout_p=dropout_p,
                causal=causal,
                softmax_scale=softmax_scale,
                group=group,
            )
    else:
        raise NotImplementedError(strategy)

    return out  # type: ignore


@torch._dynamo.disable()
def dispatch_ring_flash_attn_qkvpacked(
    qkv: torch.Tensor,
    *,
    group: dist.ProcessGroup,
    strategy: ContextParallelLoadBalancerType,
    cu_seqlens: Optional[torch.Tensor] = None,
    max_seqlen: Optional[int],
    dropout_p: float = 0.0,
    softmax_scale: Optional[float] = None,
    causal: bool = False,
) -> torch.Tensor:
    if ring_flash_attn is None:
        raise RuntimeError("flash-attn and ring-flash-attn are required!")

    if strategy == ContextParallelLoadBalancerType.zig_zag:
        if cu_seqlens is not None and max_seqlen is not None:
            out = ring_flash_attn.zigzag_ring_flash_attn_varlen_qkvpacked_func(
                _flatten_batch_dim(qkv),
                cu_seqlens,
                max_seqlen,
                dropout_p=dropout_p,
                softmax_scale=softmax_scale,
                causal=causal,
                group=group,
            )
        else:
            out = ring_flash_attn.zigzag_ring_flash_attn_qkvpacked_func(
                qkv,
                dropout_p=dropout_p,
                softmax_scale=softmax_scale,
                causal=causal,
                group=group,
            )
    else:
        raise NotImplementedError(strategy)

    return out  # type: ignore

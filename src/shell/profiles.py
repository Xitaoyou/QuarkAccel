# -*- coding: utf-8 -*-
"""profiles.py —— 四档预设。

默认「极速」= 本机实测最稳的一组（384 连接 / 1MB 切片 / 48 token），也就是
固定并发：用户设多少就开多少，不做运行时寻优。

「自适应」是独立的一档：起手 16 条，按每秒实测吞吐往上探、撞到塌陷就回撤。
它在网络抖动时更稳，但起手要爬几个窗口，小文件还没爬到就下完了 —— 所以不能
拿它当默认值（实测用户的第一反应是「改完反而没以前快」）。
"""

TIERS = [
    {
        "key": "standard",
        "name": "标准",
        "desc": "占用低，适合边下边干别的",
        "conns": 128, "part_mb": 2, "tokens": 16,
    },
    {
        "key": "turbo",
        "name": "极速",
        "desc": "实测最快最稳（推荐）",
        "conns": 384, "part_mb": 1, "tokens": 48,
        "default": True,
    },
    {
        "key": "auto",
        "name": "自适应",
        "desc": "边下边试，自动找最快的并发",
        "conns": 384, "part_mb": 1, "tokens": 48,
    },
    {
        "key": "custom",
        "name": "自定义",
        "desc": "自己调连接数与切片",
        "conns": 384, "part_mb": 1, "tokens": 32,
    },
]

DEFAULT_KEY = "turbo"

# 安全上限：实测 512 连接会 ConnectTimeout 崩，384 是稳妥上限
LIMITS = {"conns": (8, 384), "part_mb": (1, 32), "tokens": (4, 64)}


def get(key):
    for t in TIERS:
        if t["key"] == key:
            return dict(t)
    return dict(TIERS[1])


def clamp(conns, part_mb, tokens):
    (c0, c1) = LIMITS["conns"]
    (p0, p1) = LIMITS["part_mb"]
    (t0, t1) = LIMITS["tokens"]
    return (max(c0, min(c1, int(conns))),
            max(p0, min(p1, int(part_mb))),
            max(t0, min(t1, int(tokens))))


def adaptive(tier_key):
    """只有「自适应」档让并发闸门自己寻优；其余档位都是固定并发。"""
    return tier_key == "auto"


def resolve(tier_key, conns=None, part_mb=None, tokens=None):
    """返回 (conns, part_mb, tokens)。custom 档用传入值，其它档用预设值。"""
    t = get(tier_key)
    if tier_key == "custom":
        return clamp(conns if conns else t["conns"],
                     part_mb if part_mb else t["part_mb"],
                     tokens if tokens else t["tokens"])
    return (t["conns"], t["part_mb"], t["tokens"])

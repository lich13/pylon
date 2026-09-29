#!/usr/bin/env python3
"""Shared semantic value rules for Xianbao worker/report scripts."""

from __future__ import annotations

import re
from dataclasses import dataclass


MOBILE_TOPUP_CONTEXT_RE = re.compile(
    r"(手机话费|话费券|话费红包|话费立减|手机充值|充话费|移动话费|联通话费|电信话费|通信费|"
    r"(?:中国)?(?:移动|联通|电信).{0,12}(?:话费|充值|充[0-9一二两三四五六七八九十])|"
    r"(?:话费|充值|充[0-9一二两三四五六七八九十]).{0,12}(?:中国)?(?:移动|联通|电信))",
    re.I,
)


@dataclass(frozen=True)
class MobileTopupDiscountProfile:
    is_mobile_topup: bool
    parsed: bool = False
    paid: float | None = None
    face: float | None = None
    discount_rate: float | None = None
    saving: float | None = None
    poor_discount: bool = False
    no_discount: bool = False
    source: str = ""


def _round2(value: float) -> float:
    return round(float(value), 2)


def _normalize_text(text: str) -> str:
    return (
        str(text or "")
        .replace("¥", "元")
        .replace("￥", "元")
        .replace("亓", "元")
        .replace("块", "元")
        .replace("冲", "充")
        .replace(" ", "")
    )


def _profile_from_paid_face(paid: float, face: float, source: str) -> MobileTopupDiscountProfile:
    if paid <= 0 or face <= 0:
        return MobileTopupDiscountProfile(is_mobile_topup=True)
    saving = max(0.0, face - paid)
    rate = paid / face
    return MobileTopupDiscountProfile(
        is_mobile_topup=True,
        parsed=True,
        paid=_round2(paid),
        face=_round2(face),
        discount_rate=_round2(rate),
        saving=_round2(saving),
        poor_discount=rate > 0.9,
        no_discount=saving <= 0,
        source=source,
    )


def _profile_from_discount(discount: float, face: float, source: str) -> MobileTopupDiscountProfile:
    if 10 < discount < 100:
        discount = discount / 10
    if not (0 < discount < 10) or face <= 0:
        return MobileTopupDiscountProfile(is_mobile_topup=True)
    paid = face * discount / 10
    return _profile_from_paid_face(paid, face, source)


def mobile_topup_discount_profile(text: str) -> MobileTopupDiscountProfile:
    """Parse mobile top-up economics.

    A poor discount means the user pays more than 90% of the top-up face value,
    or the expression explicitly has no saving such as 100充100.
    """

    cleaned = _normalize_text(text)
    if not MOBILE_TOPUP_CONTEXT_RE.search(cleaned):
        return MobileTopupDiscountProfile(is_mobile_topup=False)

    candidates: list[MobileTopupDiscountProfile] = []

    for match in re.finditer(
        r"([0-9]+(?:\.[0-9]+)?)元?(?:充|充值)([0-9]+(?:\.[0-9]+)?)元?(?:话费|手机话费)?",
        cleaned,
        re.I,
    ):
        candidates.append(_profile_from_paid_face(float(match.group(1)), float(match.group(2)), "paid-face"))

    for match in re.finditer(
        r"(?<![0-9])([1-9][0-9]?|[1-9](?:\.[0-9]+)?)折(?:充|充值)?([0-9]+(?:\.[0-9]+)?)元?(?:话费|手机话费)?",
        cleaned,
        re.I,
    ):
        candidates.append(_profile_from_discount(float(match.group(1)), float(match.group(2)), "discount-face"))

    for match in re.finditer(
        r"(?:满|充值|充|话费)?([0-9]+(?:\.[0-9]+)?)元?(?:[-减]|立减)([0-9]+(?:\.[0-9]+)?)元?(?:话费|手机话费|话费券|立减)?",
        cleaned,
        re.I,
    ):
        face = float(match.group(1))
        saving = float(match.group(2))
        if 0 <= saving <= face:
            candidates.append(_profile_from_paid_face(face - saving, face, "minus-saving"))

    parsed = [candidate for candidate in candidates if candidate.parsed]
    if not parsed:
        return MobileTopupDiscountProfile(is_mobile_topup=True)
    return min(parsed, key=lambda candidate: candidate.discount_rate if candidate.discount_rate is not None else 99.0)


def is_poor_mobile_topup_discount(text: str) -> bool:
    profile = mobile_topup_discount_profile(text)
    return bool(profile.is_mobile_topup and profile.parsed and (profile.poor_discount or profile.no_discount))

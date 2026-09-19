"""供应商方案风险区间（临时预览）：互斥过敏原方案的必然 / 可能传播。

用户可为配方中的若干原料输入若干个**互斥**供应商方案（实际生产只会选用
其中一个），系统在**不枚举任何方案组合**的前提下，沿嵌套配料树分别
传播两种过敏原集合：

- 必然过敏原（certain）：无论最终选用哪个方案都一定存在；
- 可能过敏原（possible）：至少存在一个方案选择会引入。

关键性质：各原料的方案选择相互独立，因此每个节点上

- 自身必然 = 该节点各方案过敏原集合的**交集**；
- 自身可能 = 各方案过敏原集合的**并集**；

子配料向父配料传播时两种集合各自做并集即可——
certain(node) = own_certain ∪ ⋃ certain(child)，
possible(node) = own_possible ∪ ⋃ possible(child)，
全程只有集合交/并运算，方案数多少都不会产生组合爆炸。

对照标签声明，最终得到：

- 确定遗漏（missing_certain）：必然存在但标签未声明；
- 潜在遗漏（missing_possible）：可能存在但标签未声明（含确定遗漏）；
- 确定多报（over_reported）：声明了但所有方案下都不可能出现；
- 潜在多报（possible_over_reported）：声明了但并非所有方案都引入；
- 必然一致（matched_certain）：既必然存在又已声明。

本能力仅做临时预览：只读当前字典，不写入字典、不产生历史记录。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

from .analysis import allergen_sources, normalize_name
from .parser import IngredientNode, parse_recipe

# 方案之间用分号（中英文）分隔；方案内过敏原用逗号 / 顿号 / 空白分隔
PLAN_SPLIT = re.compile(r"[;；]")
ALLERGEN_SPLIT = re.compile(r"[,，、\s]+")

EMPTY_PLAN = "无"


class SupplierPlanError(ValueError):
    """供应商方案文本格式错误（消息可直接返回前端）。"""


@dataclass
class RiskNode:
    """配料树节点（供应商方案预览口径）。

    own_certain / own_possible —— 节点自身的必然 / 可能过敏原
        （被方案覆盖：交集 / 并集；其余原料：均取字典映射）；
    total_certain / total_possible —— 自身 + 后代传播后的汇总；
    covered —— 该原料被供应商方案覆盖（方案优先于字典）；
    unknown —— 叶子原料既未被方案覆盖、也未在字典中登记。
    """

    name: str
    children: List["RiskNode"] = field(default_factory=list)
    own_certain: Set[str] = field(default_factory=set)
    own_possible: Set[str] = field(default_factory=set)
    total_certain: Set[str] = field(default_factory=set)
    total_possible: Set[str] = field(default_factory=set)
    covered: bool = False
    unknown: bool = False

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "covered": self.covered,
            "unknown": self.unknown,
            "own_certain": sorted(self.own_certain),
            "own_possible": sorted(self.own_possible),
            "total_certain": sorted(self.total_certain),
            "total_possible": sorted(self.total_possible),
            "children": [c.to_dict() for c in self.children],
        }


# ---------- 方案文本解析 ----------

def parse_supplier_plans(text: str) -> Dict[str, List[List[str]]]:
    """解析供应商方案文本。

    每行一条：``原料：方案1；方案2``；方案内过敏原逗号分隔；
    空方案写 ``无``。# 开头为注释。

    返回 {归一化原料名: [方案（过敏原排序去重后的列表）, ...]}，
    方案按其过敏原集合去重、保留首次出现顺序；同名原料重复出现视为格式错误。
    """
    plans: Dict[str, List[List[str]]] = {}
    for lineno, raw in enumerate((text or "").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = re.split(r"[:：]", line, maxsplit=1)
        if len(parts) != 2:
            raise SupplierPlanError(
                f"第 {lineno} 行格式错误：应为「原料：方案1；方案2」（缺少冒号）：{line}")
        name = parts[0].strip()
        if not name:
            raise SupplierPlanError(f"第 {lineno} 行原料名不能为空：{line}")
        key = normalize_name(name)
        if key in plans:
            raise SupplierPlanError(f"原料「{name}」的供应商方案重复填写，请合并为一行")

        plan_sets: List[Set[str]] = []
        for segment in PLAN_SPLIT.split(parts[1]):
            token = segment.strip()
            if not token:
                raise SupplierPlanError(
                    f"原料「{name}」存在空方案：空方案请明确写「{EMPTY_PLAN}」")
            if token == EMPTY_PLAN:
                allergens: Set[str] = set()
            else:
                allergens = {a.strip() for a in ALLERGEN_SPLIT.split(token)
                             if a.strip()}
                if EMPTY_PLAN in allergens:
                    raise SupplierPlanError(
                        f"原料「{name}」的方案中「{EMPTY_PLAN}」只能单独出现，"
                        f"表示该方案不含过敏原")
            if allergens or token == EMPTY_PLAN:
                plan_sets.append(allergens)
        if not plan_sets:
            raise SupplierPlanError(f"原料「{name}」至少需要一个供应商方案")

        # 完全相同的方案去重，保留首次出现顺序（与用户填写顺序一致）
        unique: List[tuple] = []
        seen: Set[tuple] = set()
        for plan in plan_sets:
            token = tuple(sorted(plan))
            if token not in seen:
                seen.add(token)
                unique.append(token)
        plans[key] = [list(p) for p in unique]
    return plans


# ---------- 配料树标注与传播 ----------

def _annotate(nodes: List[IngredientNode],
              plans: Dict[str, List[List[str]]],
              dictionary: Dict[str, Set[str]]) -> List[RiskNode]:
    """递归构造风险树：按方案 / 字典标注自身过敏原，并向上传播。

    不枚举任何方案组合：方案覆盖原料的自身必然 / 可能直接取各方案的
    交集 / 并集，其余原料两者相同（均取字典映射）；子配料汇总时
    两种集合分别做并集。
    """
    out: List[RiskNode] = []
    for node in nodes:
        key = normalize_name(node.name)
        children = _annotate(node.children, plans, dictionary)
        rn = RiskNode(name=node.name, children=children)
        if key in plans:
            option_sets = [set(p) for p in plans[key]]
            # 各方案独立，必选 = 交集；可选 = 并集
            rn.own_certain = set.intersection(*option_sets) if option_sets else set()
            rn.own_possible = set.union(*option_sets) if option_sets else set()
            rn.covered = True
        else:
            # 未覆盖原料沿用现有字典
            own = set(dictionary.get(key, set()))
            rn.own_certain = set(own)
            rn.own_possible = set(own)
            rn.unknown = key not in dictionary and not children
        rn.total_certain = set(rn.own_certain)
        rn.total_possible = set(rn.own_possible)
        for child in children:
            rn.total_certain |= child.total_certain
            rn.total_possible |= child.total_possible
        out.append(rn)
    return out


def _collect(nodes: List[RiskNode], attr: str) -> Set[str]:
    result: Set[str] = set()
    for node in nodes:
        result |= getattr(node, attr)
    return result


def _unknown_leaves(nodes: List[RiskNode]) -> List[str]:
    names: Set[str] = set()
    for node in nodes:
        if node.unknown:
            names.add(node.name)
        names |= set(_unknown_leaves(node.children))
    return sorted(names)


def preview_supplier_plans(*, text: str, plans_text: str,
                           declared: List[str],
                           dictionary: Optional[Dict[str, Set[str]]] = None,
                           plan_overrides: Optional[Dict[str, List[List[str]]]] = None
                           ) -> dict:
    """构建供应商方案风险区间预览结果（不落库、不写字典）。

    plan_overrides 可直接传入已解析方案（测试用）；否则解析 plans_text。
    """
    from . import db  # 局部导入：避免核心计算与持久层强耦合
    dictionary = db.dict_lookup() if dictionary is None else dictionary
    plans = parse_supplier_plans(plans_text) if plan_overrides is None else plan_overrides

    source_tree = parse_recipe(text)
    tree = _annotate(source_tree, plans, dictionary)
    tree_dicts = [n.to_dict() for n in tree]

    certain = _collect(tree, "total_certain")
    possible = _collect(tree, "total_possible")
    declared_set = set(declared)

    missing_certain = sorted(certain - declared_set)
    missing_possible = sorted(possible - declared_set)
    over_reported = sorted(declared_set - possible)
    possible_over_reported = sorted(declared_set & (possible - certain))
    matched_certain = sorted(certain & declared_set)

    # 两类遗漏分别给出配料树来源路径：必然取自身必选标注，潜在取自身可选标注
    result = {
        "tree": tree_dicts,
        "certain_allergens": sorted(certain),
        "possible_allergens": sorted(possible),
        "unknown_ingredients": _unknown_leaves(tree),
        "label_check": {
            "missing_certain": missing_certain,
            "missing_possible": missing_possible,
            "over_reported": over_reported,
            "possible_over_reported": possible_over_reported,
            "matched_certain": matched_certain,
            "missing_certain_sources": allergen_sources(
                tree_dicts, missing_certain, "own_certain"),
            "missing_possible_sources": allergen_sources(
                tree_dicts, missing_possible, "own_possible"),
        },
    }

    # 方案回显（保留方案输入顺序），并标注配方中是否实际用到
    flat = _flatten_names(source_tree)
    name_by_key: Dict[str, str] = {}
    for n in flat:
        name_by_key.setdefault(normalize_name(n.name), n.name)
    seen_keys = set(name_by_key)
    ingredients = []
    for key, option_lists in plans.items():
        ingredients.append({
            "name": name_by_key.get(key, key),
            "plans": option_lists,
            "used_in_recipe": key in seen_keys,
        })
    ingredients.sort(key=lambda x: normalize_name(x["name"]))
    result["supplier_ingredients"] = ingredients
    result["warnings"] = [
        f"原料「{item['name']}」填写了供应商方案，但配方中未出现该原料，预览未采用"
        for item in ingredients if not item["used_in_recipe"]]
    return result


def _flatten_names(nodes: List[IngredientNode]) -> List[IngredientNode]:
    out: List[IngredientNode] = []
    for node in nodes:
        out.append(node)
        out.extend(_flatten_names(node.children))
    return out

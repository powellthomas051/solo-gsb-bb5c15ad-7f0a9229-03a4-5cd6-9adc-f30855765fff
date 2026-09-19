"""供应商方案风险区间预览测试。

覆盖：
- 方案文本解析（分隔符、空方案「无」、去重、格式错误）；
- 不枚举组合：嵌套树上必然（交集）/ 可能（并集）独立传播；
- 确定遗漏 / 潜在遗漏 / 确定多报 / 潜在多报 / 必然一致的分区；
- 两类遗漏各自的来源路径；
- 只有一个方案时结果与现有审查完全一致；
- 未覆盖原料沿用字典、方案覆盖原料优先于字典。
"""
import unittest

from app.analysis import (attach_allergens, collect_allergens, compare_label,
                          normalize_name)
from app.parser import parse_recipe
from app.supplier import (SupplierPlanError, parse_supplier_plans,
                          preview_supplier_plans)


def lookup(d):
    return {normalize_name(k): set(v) for k, v in d.items()}


DICT = {
    "小麦粉": ["麸质谷物"],
    "乳清粉": ["乳"],
    "奶粉": ["乳"],
    "可可脂": [],
    "鸡蛋": ["蛋类"],
    "食盐": [],
}


class ParsePlansTest(unittest.TestCase):
    def test_basic_parse(self):
        plans = parse_supplier_plans("奶粉：乳；无\n香精：花生，乳；大豆")
        self.assertEqual(plans[normalize_name("奶粉")], [["乳"], []])
        self.assertEqual(plans[normalize_name("香精")],
                         [["乳", "花生"], ["大豆"]])

    def test_english_punctuation(self):
        plans = parse_supplier_plans("奶粉: 乳 ; 无")
        self.assertEqual(plans[normalize_name("奶粉")], [["乳"], []])

    def test_comment_and_blank_lines(self):
        plans = parse_supplier_plans("# 注释\n\n奶粉：乳\n")
        self.assertEqual(set(plans), {normalize_name("奶粉")})

    def test_duplicate_plans_deduped(self):
        plans = parse_supplier_plans("奶粉：乳，大豆；大豆，乳；无")
        self.assertEqual(plans[normalize_name("奶粉")],
                         [["乳", "大豆"], []])

    def test_empty_plan_must_say_wu(self):
        with self.assertRaises(SupplierPlanError):
            parse_supplier_plans("奶粉：乳；")
        with self.assertRaises(SupplierPlanError):
            parse_supplier_plans("奶粉：")
        # 「无」与其他过敏原混写不允许
        with self.assertRaises(SupplierPlanError):
            parse_supplier_plans("奶粉：无，乳")

    def test_missing_colon(self):
        with self.assertRaises(SupplierPlanError):
            parse_supplier_plans("奶粉 乳；无")

    def test_duplicate_ingredient_line(self):
        with self.assertRaises(SupplierPlanError):
            parse_supplier_plans("奶粉：乳\n奶粉：无")


class PropagationTest(unittest.TestCase):
    def setUp(self):
        self.dictionary = lookup(DICT)

    def test_intersection_union_without_enumeration(self):
        # 奶粉方案：乳 / 无；香精方案：花生,乳 / 大豆
        # 不枚举 2x2 组合：交集只有“无”，并集为 {乳, 花生, 大豆}
        r = preview_supplier_plans(
            text="配料表：小麦粉，巧克力酱（奶粉，可可脂，香精），鸡蛋",
            plans_text="奶粉：乳；无\n香精：花生，乳；大豆",
            declared=[], dictionary=self.dictionary)
        self.assertEqual(r["certain_allergens"], ["蛋类", "麸质谷物"])
        self.assertEqual(r["possible_allergens"],
                         ["乳", "大豆", "花生", "蛋类", "麸质谷物"])

        tree = {n["name"]: n for n in r["tree"]}
        choco = tree["巧克力酱"]
        self.assertEqual(choco["total_certain"], [])
        self.assertEqual(sorted(choco["total_possible"]),
                         ["乳", "大豆", "花生"])

        milk = next(c for c in choco["children"] if c["name"] == "奶粉")
        self.assertTrue(milk["covered"])
        self.assertEqual(milk["own_certain"], [])
        self.assertEqual(milk["own_possible"], ["乳"])

    def test_certain_through_nested_levels(self):
        # 深层嵌套：必然过敏原必须逐层传到顶层
        r = preview_supplier_plans(
            text="酱（奶粉（乳清粉），香精）",
            plans_text="乳清粉：乳，大豆；乳\n香精：大豆；无",
            declared=[], dictionary=self.dictionary)
        # 乳清粉各方案交集 = {乳}；香精交集为空
        self.assertIn("乳", r["certain_allergens"])
        self.assertNotIn("大豆", r["certain_allergens"])
        self.assertIn("大豆", r["possible_allergens"])
        root = r["tree"][0]
        self.assertEqual(root["total_certain"], ["乳"])

    def test_independent_sibling_choices(self):
        # 两个互不相干的原料各带方案：必然 = 两边必然之并，
        # 不会因为“方案错配”而错误地把仅可能项算成不可能或必然
        r = preview_supplier_plans(
            text="奶粉，香精",
            plans_text="奶粉：乳；无\n香精：大豆；无",
            declared=[], dictionary=self.dictionary)
        self.assertEqual(r["certain_allergens"], [])
        self.assertEqual(sorted(r["possible_allergens"]), ["乳", "大豆"])

    def test_uncovered_ingredients_follow_dictionary(self):
        r = preview_supplier_plans(
            text="小麦粉，奶粉，神秘香精",
            plans_text="奶粉：乳；无",
            declared=[], dictionary=self.dictionary)
        # 小麦粉沿用字典（必然含麸质）；神秘香精未登记且未覆盖 -> 未识别
        self.assertIn("麸质谷物", r["certain_allergens"])
        self.assertEqual(r["unknown_ingredients"], ["神秘香精"])
        milk = next(n for n in r["tree"] if n["name"] == "奶粉")
        self.assertTrue(milk["covered"])
        flour = next(n for n in r["tree"] if n["name"] == "小麦粉")
        self.assertFalse(flour["covered"])

    def test_supplier_plan_overrides_dictionary(self):
        # 字典里奶粉含乳；供应商唯一方案写「无」时以方案为准
        r = preview_supplier_plans(
            text="奶粉", plans_text="奶粉：无",
            declared=[], dictionary=self.dictionary)
        self.assertEqual(r["certain_allergens"], [])
        self.assertEqual(r["possible_allergens"], [])


class LabelClassificationTest(unittest.TestCase):
    def setUp(self):
        self.dictionary = lookup(DICT)

    def _preview(self, declared):
        return preview_supplier_plans(
            text="配料表：小麦粉，巧克力酱（奶粉，可可脂，香精），鸡蛋",
            plans_text="奶粉：乳；无\n香精：花生，乳；大豆",
            declared=declared, dictionary=self.dictionary)

    def test_buckets(self):
        lc = self._preview(["麸质谷物", "蛋类", "坚果"])["label_check"]
        # 确定遗漏：必然存在且未声明（麸质、蛋类已声明）
        self.assertEqual(lc["missing_certain"], [])
        # 潜在遗漏：乳 / 花生 / 大豆仅部分方案引入且未声明
        self.assertEqual(lc["missing_possible"], ["乳", "大豆", "花生"])
        # 确定多报：坚果任何方案下都不可能出现
        self.assertEqual(lc["over_reported"], ["坚果"])
        # 潜在多报：没有“已声明且仅部分方案出现”的项
        self.assertEqual(lc["possible_over_reported"], [])
        self.assertEqual(lc["matched_certain"], ["蛋类", "麸质谷物"])

    def test_possible_over_reported_bucket(self):
        # 乳声明了：部分方案含乳 -> 潜在多报；麸质必然且声明 -> 必然一致
        lc = self._preview(["麸质谷物", "蛋类", "乳"])["label_check"]
        self.assertEqual(lc["missing_possible"], ["大豆", "花生"])
        self.assertEqual(lc["over_reported"], [])
        self.assertEqual(lc["possible_over_reported"], ["乳"])
        self.assertEqual(lc["matched_certain"], ["蛋类", "麸质谷物"])

    def test_missing_certain_bucket_and_sources(self):
        lc = self._preview([])["label_check"]
        self.assertEqual(lc["missing_certain"], ["蛋类", "麸质谷物"])
        # 确定遗漏来源：终止于自身必然标注的原料
        self.assertEqual(lc["missing_certain_sources"]["麸质谷物"],
                         [["小麦粉"]])
        self.assertEqual(lc["missing_certain_sources"]["蛋类"], [["鸡蛋"]])

    def test_missing_possible_sources(self):
        lc = self._preview(["麸质谷物", "蛋类"])["label_check"]
        sources = lc["missing_possible_sources"]
        # 乳：奶粉方案 1 含乳（own_possible），香精方案 1 也含乳，
        # 两条分支路径分别保留
        self.assertEqual(sources["乳"], [
            ["巧克力酱", "奶粉"],
            ["巧克力酱", "香精"],
        ])
        self.assertEqual(sources["花生"], [["巧克力酱", "香精"]])
        self.assertEqual(sources["大豆"], [["巧克力酱", "香精"]])
        # 必然遗漏同时在潜在遗漏中，但潜在来源还包含“仅可能”的携带点
        lc2 = self._preview([])["label_check"]
        # 蛋类自身必然 -> 既是确定来源也是可能来源
        self.assertEqual(lc2["missing_possible_sources"]["蛋类"], [["鸡蛋"]])

    def test_possible_range_contains_certain(self):
        # 潜在遗漏必须包含确定遗漏（可能集合是必然集合的超集）
        lc = self._preview([])["label_check"]
        self.assertTrue(set(lc["missing_certain"]) <= set(lc["missing_possible"]))
        self.assertEqual(set(lc["missing_certain"]), {"蛋类", "麸质谷物"})

    def test_compound_ingredient_covered_by_plans(self):
        # 复合父配料「巧克力酱」本身被方案覆盖：自身标注 + 子配料传播同时生效
        r = preview_supplier_plans(
            text="巧克力酱（奶粉）",
            plans_text="巧克力酱：坚果；无\n奶粉：乳；无",
            declared=[], dictionary=self.dictionary)
        root = r["tree"][0]
        self.assertTrue(root["covered"])
        self.assertEqual(root["own_certain"], [])
        self.assertEqual(sorted(root["own_possible"]), ["坚果"])
        self.assertEqual(sorted(root["total_possible"]), ["乳", "坚果"])

    def test_warning_for_unused_supplier_ingredient(self):
        r = preview_supplier_plans(
            text="奶粉", plans_text="奶粉：乳；无\n芝麻：芝麻；无",
            declared=[], dictionary=self.dictionary)
        self.assertEqual(len(r["warnings"]), 1)
        self.assertIn("芝麻", r["warnings"][0])
        items = {i["name"]: i for i in r["supplier_ingredients"]}
        self.assertFalse(items["芝麻"]["used_in_recipe"])
        self.assertTrue(items["奶粉"]["used_in_recipe"])


class SinglePlanEquivalenceTest(unittest.TestCase):
    """只有一个方案时，风险区间预览必须与现有审查结论一致。"""

    def setUp(self):
        self.dictionary = lookup({
            **DICT,
            "大豆卵磷脂": ["大豆"],
            "白砂糖": [],
        })

    def test_matches_standard_review(self):
        text = ("配料表：小麦粉，巧克力酱（奶粉（乳清粉），可可脂，"
                "大豆卵磷脂），鸡蛋，白砂糖，食盐")
        plans = {"奶粉": [["乳"]], "白砂糖": [["花生"]]}
        declared = ["麸质谷物", "蛋类"]
        r = preview_supplier_plans(
            text=text, plans_text="", declared=declared,
            dictionary=self.dictionary, plan_overrides=plans)

        # 对照：把方案覆盖视为字典替换后走现有审查口径
        merged = {k: set(v) for k, v in self.dictionary.items()}
        merged[normalize_name("奶粉")] = {"乳"}
        merged[normalize_name("白砂糖")] = {"花生"}
        tree = parse_recipe(text)
        attach_allergens(tree, merged)
        standard = compare_label(collect_allergens(tree), declared,
                                 [n.to_dict() for n in tree])

        self.assertEqual(set(r["certain_allergens"]),
                         set(collect_allergens(tree)))
        self.assertEqual(set(r["possible_allergens"]),
                         set(r["certain_allergens"]))
        lc = r["label_check"]
        self.assertEqual(lc["missing_certain"], standard["missing"])
        self.assertEqual(lc["missing_possible"], standard["missing"])
        self.assertEqual(lc["over_reported"], standard["over_reported"])
        self.assertEqual(lc["matched_certain"], standard["matched"])
        self.assertEqual(lc["possible_over_reported"], [])
        self.assertEqual(lc["missing_certain_sources"],
                         standard["missing_sources"])
        self.assertEqual(lc["missing_possible_sources"],
                         standard["missing_sources"])

    def test_no_plans_at_all_matches_review(self):
        text = "小麦粉，奶粉，鸡蛋"
        r = preview_supplier_plans(
            text=text, plans_text="", declared=["麸质谷物"],
            dictionary=self.dictionary)
        tree = parse_recipe(text)
        attach_allergens(tree, self.dictionary)
        standard = compare_label(collect_allergens(tree), ["麸质谷物"],
                                 [n.to_dict() for n in tree])
        self.assertEqual(r["label_check"]["missing_certain"],
                         standard["missing"])
        self.assertEqual(r["label_check"]["missing_certain_sources"],
                         standard["missing_sources"])


if __name__ == "__main__":
    unittest.main()

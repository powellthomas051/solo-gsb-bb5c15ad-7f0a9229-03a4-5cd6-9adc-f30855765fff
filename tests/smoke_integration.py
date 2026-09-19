"""端到端集成冒烟测试：不依赖 FastAPI，直接走 db + parser + analysis + report 全链路。"""
import os
import tempfile

os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "smoke.db")

from app import db
from app.analysis import (attach_allergens, collect_allergens, compare_label,
                          diff_allergens, diff_ingredients, flatten)
from app.fingerprint import result_keys, scoped_fingerprint
from app.parser import parse_recipe
from app.report import render_html_report, render_markdown_report

db.init_db()

# 1. 导入字典
n = db.bulk_import("""\
小麦粉：麸质谷物
乳清粉：乳
乳糖：乳
奶粉：乳
可可脂：
大豆卵磷脂：大豆
鸡蛋：蛋类
椰子粉：坚果
芝麻：芝麻
食盐：
""")
assert n == 10, n
print(f"1. 字典导入 {n} 条 OK")

# 2. 两版配方
TEXT_A = "配料表：小麦粉，白砂糖，巧克力酱（可可脂，奶粉（乳清粉，乳糖），大豆卵磷脂），鸡蛋，食盐"
TEXT_B = "配料表：小麦粉，白砂糖，巧克力酱（可可脂，椰子粉），鸡蛋，芝麻酱（芝麻），食盐"
DECLARED = ["麸质谷物", "蛋类"]

dictionary = db.dict_lookup()

def build_version(tree):
    return {
        "tree": [x.to_dict() for x in tree],
        "allergens": sorted(collect_allergens(tree)),
        "unknown_ingredients": sorted({x.name for x in flatten(tree) if x.unknown}),
    }

tree_a = parse_recipe(TEXT_A)
attach_allergens(tree_a, dictionary)
va = build_version(tree_a)
tree_b = parse_recipe(TEXT_B)
attach_allergens(tree_b, dictionary)
vb = build_version(tree_b)

assert set(va["allergens"]) == {"麸质谷物", "乳", "大豆", "蛋类"}, va["allergens"]
assert set(vb["allergens"]) == {"麸质谷物", "坚果", "蛋类", "芝麻"}, vb["allergens"]
assert "白砂糖" in va["unknown_ingredients"]  # 未登记原料被标记
print("2. 两版配方解析与过敏原传播 OK")
print("   A 版过敏原:", va["allergens"])
print("   B 版过敏原:", vb["allergens"])

# 3. 标签核对（对照 B 版，含遗漏来源路径）
check = compare_label(vb["allergens"], DECLARED, vb["tree"])
assert check["missing"] == ["坚果", "芝麻"], check
assert check["over_reported"] == [], check
assert check["matched"] == ["蛋类", "麸质谷物"], check
assert check["missing_sources"]["坚果"] == [["巧克力酱", "椰子粉"]], check
assert check["missing_sources"]["芝麻"] == [["芝麻酱", "芝麻"]], check
print("3. 标签核对 OK  遗漏:", check["missing"], " 多报:", check["over_reported"])
print("   来源路径:", check["missing_sources"])

# 3b. 同名原料处于不同分支时来源路径分别保留，不合并
tree_c = parse_recipe("巧克力酱（奶粉（乳清粉）），夹心酱（奶粉（乳清粉））")
attach_allergens(tree_c, dictionary)
vc = build_version(tree_c)
check_c = compare_label(vc["allergens"], [], vc["tree"])
assert check_c["missing_sources"]["乳"] == [
    ["夹心酱", "奶粉"],
    ["夹心酱", "奶粉", "乳清粉"],
    ["巧克力酱", "奶粉"],
    ["巧克力酱", "奶粉", "乳清粉"],
], check_c["missing_sources"]
# 父节点自身标注与子配料传播各产生一条路径；同名分支分别保留、不合并
print("3b. 同名分支路径不合并 / 自身标注+子配料传播 OK")

# 4. 版本对比
diff = diff_allergens(va["allergens"], vb["allergens"])
assert diff["added"] == ["坚果", "芝麻"]
assert diff["removed"] == ["乳", "大豆"]
changes = diff_ingredients(tree_a, tree_b)
names = {c["ingredient"]: c["change"] for c in changes}
assert names.get("巧克力酱") == "modified" and names.get("芝麻酱") == "added", names
print("4. 版本对比 OK  新增:", diff["added"], " 减少:", diff["removed"])

# 5. 入库 + 报告导出
result = {
    "version_a": va, "version_b": vb, "diff": diff,
    "ingredient_changes": changes,
    "label_check": check, "label_check_version": "新版配方",
}
rid = db.create_review(name="巧克力饼干 配方变更审查", declared=DECLARED,
                       text_a=TEXT_A, text_b=TEXT_B,
                       label_a="现版配方", label_b="新版配方", result=result,
                       dict_fingerprint=scoped_fingerprint(
                           result_keys(result), dictionary))
review = db.get_review(rid)
html_report = render_html_report(review)
md_report = render_markdown_report(review)
assert "标签遗漏" in html_report and "坚果" in html_report and "芝麻" in html_report
assert "巧克力酱" in html_report and "来源路径" in html_report
assert "巧克力酱 › 椰子粉" in md_report and "芝麻酱 › 芝麻" in md_report
assert "# 食品过敏原标签审查报告" in md_report and "新增过敏原" in md_report
assert len(db.list_reviews()) == 1

# 5b. 模拟升级前的旧记录（结果中无 missing_sources），重新打开时自动补齐
old_result = dict(result)
old_result["label_check"] = {k: v for k, v in check.items()
                             if k != "missing_sources"}
old_rid = db.create_review(name="旧版记录", declared=DECLARED,
                           text_a=TEXT_A, text_b=TEXT_B,
                           label_a="现版配方", label_b="新版配方",
                           result=old_result)
old_review = db.get_review(old_rid)
assert old_review["result"]["label_check"]["missing_sources"]["坚果"] == \
    [["巧克力酱", "椰子粉"]]
print(f"5. 审查记录入库 (#{rid}) 与 HTML/Markdown 报告导出 OK；旧记录回填 OK")

# 6. 字典时效复核
from app.recheck import build_recheck

# 6a. 刚创建的记录指纹与当前字典一致，时效正常
assert db.get_review(rid)["needs_recheck"] is False

# 6b. 无关原料增删 / 顺序变化 / 同值重导不得误报
db.upsert_entry("无关原料", ["芥末"])
assert db.get_review(rid)["needs_recheck"] is False
db.delete_entry("无关原料")
db.bulk_import("\n".join(reversed("""\
小麦粉：麸质谷物
乳清粉：乳
乳糖：乳
奶粉：乳
可可脂：
大豆卵磷脂：大豆
鸡蛋：蛋类
椰子粉：坚果
芝麻：芝麻
食盐：
""".strip().splitlines())))
assert db.get_review(rid)["needs_recheck"] is False, "顺序/无关变化不得误报"

# 6c. 配方实际涉及原料的映射变化 -> 需复核（两版取并集，椰子粉仅在 B 版）
db.upsert_entry("椰子粉", ["坚果", "大豆"])
stale = db.get_review(rid)
assert stale["needs_recheck"] and stale["recheck_reason"] == "stale"
outcome = build_recheck(stale)
assert outcome["changed"]
# B 版（核对对象）新增遗漏“大豆”，来源路径为 巧克力酱 › 椰子粉
assert "大豆" in outcome["diff"]["label"]["missing_delta"]["added"]
assert ["巧克力酱", "椰子粉"] in \
    outcome["new_result"]["label_check"]["missing_sources"]["大豆"]

# 6d. 确认复核 -> 覆盖结果、刷新指纹、恢复时效正常
db.update_review_result(rid, outcome["new_result"], outcome["fingerprint"])
assert db.get_review(rid)["needs_recheck"] is False
# 复核后报告可继续导出，且包含新结论
assert "大豆" in render_markdown_report(db.get_review(rid))

# 6e. 升级前旧记录无指纹 -> 按需复核；复核确认后补上指纹
assert db.get_review(old_rid)["needs_recheck"] is True
assert db.get_review(old_rid)["recheck_reason"] == "legacy"
legacy_outcome = build_recheck(db.get_review(old_rid))
db.update_review_result(old_rid, legacy_outcome["new_result"],
                        legacy_outcome["fingerprint"])
assert db.get_review(old_rid)["needs_recheck"] is False
print("6. 字典时效复核（指纹/误报防护/前后差异/确认覆盖/旧记录） OK")

# 7. 供应商方案风险区间（临时预览：不写库、不留历史、不枚举组合）
from app.supplier import (SupplierPlanError, parse_supplier_plans,
                          preview_supplier_plans)

reviews_before = len(db.list_reviews())
dict_before = {e["ingredient"]: e["allergens"] for e in db.list_dict()}

# 7a. 方案解析：分号分隔方案、逗号分隔过敏原、空方案写「无」、重复方案去重
parsed = parse_supplier_plans("奶粉：乳，大豆；大豆，乳；无\n香精: 花生; 无")
assert parsed["奶粉"] == [["乳", "大豆"], []], parsed["奶粉"]
assert parsed["香精"] == [["花生"], []], parsed["香精"]
try:
    parse_supplier_plans("奶粉：乳；")  # 空方案必须写「无」
    raise AssertionError("应当报格式错误")
except SupplierPlanError:
    pass

# 7b. 不枚举组合：嵌套树上必然（交集）/ 可能（并集）分别传播
sp_text = "配料表：小麦粉，巧克力酱（奶粉，可可脂，香精），鸡蛋"
sp = preview_supplier_plans(
    text=sp_text,
    plans_text="奶粉：乳；无\n香精：花生，乳；大豆",
    declared=["麸质谷物", "蛋类", "坚果"],
    dictionary=dictionary)
assert sp["certain_allergens"] == ["蛋类", "麸质谷物"], sp["certain_allergens"]
assert sp["possible_allergens"] == ["乳", "大豆", "花生", "蛋类", "麸质谷物"], \
    sp["possible_allergens"]
lc = sp["label_check"]
assert lc["missing_certain"] == [], lc
assert lc["missing_possible"] == ["乳", "大豆", "花生"], lc
assert lc["over_reported"] == ["坚果"], lc                 # 任何方案都不可能
assert lc["possible_over_reported"] == [], lc
assert lc["matched_certain"] == ["蛋类", "麸质谷物"], lc
# 两类遗漏的来源路径分别保留（乳由两个被覆盖原料各自的方案引入）
assert lc["missing_possible_sources"]["乳"] == [
    ["巧克力酱", "奶粉"], ["巧克力酱", "香精"]], lc["missing_possible_sources"]
assert lc["missing_possible_sources"]["花生"] == [["巧克力酱", "香精"]]

# 7c. 只有一个方案时与现有审查口径一致
single = preview_supplier_plans(
    text=sp_text, plans_text="香精：大豆",
    declared=["麸质谷物", "蛋类", "坚果"],
    dictionary=dictionary)
tree_s = parse_recipe(sp_text)
merged = dict(dictionary)
merged["香精"] = {"大豆"}
attach_allergens(tree_s, merged)
vs = build_version(tree_s)
std_check = compare_label(vs["allergens"], ["麸质谷物", "蛋类", "坚果"], vs["tree"])
assert single["certain_allergens"] == vs["allergens"]
assert single["label_check"]["missing_certain"] == std_check["missing"]
assert single["label_check"]["missing_certain_sources"] == \
    std_check["missing_sources"]

# 7d. 临时预览：字典与历史记录均无变化
assert len(db.list_reviews()) == reviews_before
assert {e["ingredient"]: e["allergens"] for e in db.list_dict()} == dict_before
print("7. 供应商方案风险区间（交集/并集传播、五类结论、来源路径、"
      "单方案一致性、不落库） OK")

print("\n全部集成冒烟测试通过 ✔")

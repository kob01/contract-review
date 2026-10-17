"""生成覆盖 22 条内置红线的合同样本，并用真实规则引擎自检触发情况。

样本不是给人读的散文，而是回归夹具：每条 POS 样本声明"应当触发哪些红线"，
每条 NEG 样本声明"一条都不应当触发"。任何一条对不上就应当认为正则或规则改了。

    uv run python ../scripts/gen_test_contracts.py            # 生成 + 校验
    uv run python ../scripts/gen_test_contracts.py --check    # 只校验已有文件

`--check` 会跳过写文件，直接读 `testdata/contracts/` 下的 .txt 做断言，
所以删掉本脚本后覆盖矩阵仍然可以独立复跑。
"""

from __future__ import annotations

import sys
from pathlib import Path

from app.models.redline import RedLineRule
from app.services.contract_service import infer_contract_kind
from app.services.rules.builtin_redlines import as_rule_rows
from app.services.rules.engine import RuleEngine
from app.services.rules.metrics import extract_metrics
from app.services.parser.extractor import ExtractedDocument
from app.services.parser.segmenter import segmentize
from tests.fixtures import build_document

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "testdata" / "contracts"

#: 所有 POS 样本共用的"无风险正文"，用来把非目标红线压在阈值以下。
#: 刻意不含"每期还款金额"，否则 IRR 会被算出来并连带触发 RATE-005。
CLEAN_LOAN = """第一条 借款金额与期限
借款金额为 50,000 元，借款期限 12 个月。

第二条 利率与费用
本合同年利率为 12%，以年化形式明示。除利息外甲方不收取服务费、担保费或咨询费。

第三条 还款
到期一次性归还本金，并按实际用款天数支付利息。
"""

HEADER = """{title}

甲方（{party_a_label}）：{party_a}
乙方（借款人）：{party_b}

"""


def sample(title: str, party_a_label: str, party_a: str, body: str, party_b: str = "某某") -> str:
    return HEADER.format(
        title=title, party_a_label=party_a_label, party_a=party_a, party_b=party_b
    ) + body


#: (文件名, 期望合同种类, 期望命中的红线编码, 正文)
#: 期望命中为空集表示这是一份合规对照样本，任何命中都是误报。
SAMPLES: list[tuple[str, str, set[str], str]] = [
    # ---------------- 利率与费用 ----------------
    (
        "POS-01_RATE-001_综合年化成本28.txt",
        "consumer_loan",
        {"RATE-001"},
        sample(
            "个人消费借款合同",
            "贷款人",
            "幸福消费金融股份有限公司",
            """第一条 借款金额与期限
借款金额为 50,000 元，借款期限 12 个月。

第二条 利率
本合同综合年化融资成本为 28%，按日计息、按月付息。

第三条 还款
到期一次性归还本金并结息。
""",
        ),
    ),
    (
        "POS-02_RATE-002_民间借贷年利率13.txt",
        "private_lending",
        {"RATE-002"},
        sample(
            "民间借款合同",
            "出借人",
            "王某某",
            """第一条 借款金额
借款金额为 200,000 元。

第二条 利息
双方约定年利率为 13%，借款期限内不变。本利率以合同成立时全国银行间同业拆借中心
公布的一年期贷款市场报价利率（LPR）为参照。

第三条 还款
借款期限 12 个月，到期一次性归还本金及利息。

第四条 争议解决
因本合同发生的争议，由借款人住所地人民法院管辖。
""",
            party_b="陈某某",
        ),
    ),
    (
        "POS-03_RATE-003_月利率3分5.txt",
        "consumer_loan",
        {"RATE-001", "RATE-003"},
        sample(
            "借款合同",
            "贷款人",
            "惠民小额贷款有限公司",
            """第一条 借款金额与期限
借款金额为 30,000 元，借款期限 6 个月。

第二条 利率
本合同月利率为 3.5%，利息按月收取，到期归还本金。
""",
            party_b="赵某某",
        ),
    ),
    (
        "POS-04_RATE-004_只写费用不写年化.txt",
        "consumer_loan",
        {"RATE-004"},
        sample(
            "个人借款合同",
            "出借人",
            "张某某",
            """第一条 借款金额
借款金额为 10,000 元。

第二条 费用
乙方于借款时向甲方支付一次性居间服务费 800 元，用于甲方办理本笔借款的手续。

第三条 还款
借款到期时乙方一次性归还本金，借款期限以双方另行确认为准。

第四条 其他
本合同未尽事宜由双方协商解决。
""",
            party_b="刘某某",
        ),
    ),
    (
        "POS-05_RATE-005_名义8实际IRR29.txt",
        "consumer_loan",
        {"RATE-001", "RATE-005"},
        sample(
            "个人消费分期借款合同",
            "贷款人",
            "某某消费金融有限公司",
            """第一条 借款金额与期限
借款金额为 100,000 元，借款期限 12 个月。

第二条 名义利率
本合同名义年利率为 8%，利息按单利计收。

第三条 还款
每期还款金额 9,500 元，等额本息偿还。
""",
            party_b="孙某某",
        ),
    ),
    (
        "POS-06_FEE-001_砍头息预扣12.txt",
        "consumer_loan",
        {"FEE-001"},
        sample(
            "借款合同",
            "出借人",
            "周某某",
            """第一条 借款金额
借款金额为 100,000 元。

第二条 放款
甲方在放款时预先扣除利息及服务费，实际放款金额为 88,000 元。

第三条 期限与利率
借款期限 12 个月，年利率为 10%，到期按实际放款金额偿还本息。
""",
            party_b="吴某某",
        ),
    ),
    (
        "POS-07_FEE-002_服务费另行收取.txt",
        "consumer_loan",
        {"FEE-002"},
        sample(
            "个人消费贷款合同",
            "贷款人",
            "某消费金融有限公司",
            """第一条 借款金额与期限
借款金额为 80,000 元，借款期限 24 个月。

第二条 利率
本合同年利率为 9.6%，到期归还本金。

第三条 其他费用
除利息外，借款服务费另行收取，按季支付；账户管理费单独收取。
""",
            party_b="郑某某",
        ),
    ),
    # ---------------- 消费者保护与格式条款 ----------------
    (
        "POS-08_PROT-001_最终解释权归甲方.txt",
        "consumer_loan",
        {"PROT-001"},
        sample(
            "消费借款合同",
            "贷款人",
            "某某消费金融有限公司",
            CLEAN_LOAN
            + """
第四条 附则
本合同条款的最终解释权归甲方所有。
""",
        ),
    ),
    (
        "POS-09_PROT-002_不得提出异议.txt",
        "consumer_loan",
        {"PROT-002"},
        sample(
            "消费借款合同",
            "贷款人",
            "某某消费金融有限公司",
            CLEAN_LOAN
            + """
第四条 逾期处理
乙方对甲方计算的应付款项有异议的，仍应先付款，且不得对甲方的计息方式提出异议。
""",
        ),
    ),
    (
        "POS-10_PROT-003_有权随时调整利率.txt",
        "consumer_loan",
        {"PROT-003"},
        sample(
            "消费借款合同",
            "贷款人",
            "某某消费金融有限公司",
            CLEAN_LOAN
            + """
第四条 费率调整
甲方有权随时调整利率与收费标准，调整后的标准自甲方公布之日起适用。
""",
        ),
    ),
    (
        "POS-11_PROT-004_概括扣划授权.txt",
        "consumer_loan",
        {"PROT-004"},
        sample(
            "消费借款合同",
            "贷款人",
            "某某消费金融有限公司",
            CLEAN_LOAN
            + """
第四条 扣款授权
乙方不可撤销地授权甲方从乙方任一银行账户直接扣划应付款项，无需另行通知。
""",
        ),
    ),
    (
        "POS-12_PROT-005_必须投保否则不放款.txt",
        "consumer_loan",
        {"PROT-005"},
        sample(
            "消费借款合同",
            "贷款人",
            "某某消费金融有限公司",
            CLEAN_LOAN
            + """
第四条 保险
乙方必须投保借款人意外伤害保险，投保完成为甲方放款的前提条件。
""",
        ),
    ),
    (
        "POS-13_INFO-001_向合作机构共享个人信息.txt",
        "consumer_loan",
        {"INFO-001"},
        sample(
            "消费借款合同",
            "贷款人",
            "某某消费金融有限公司",
            CLEAN_LOAN
            + """
第四条 信息使用
乙方同意向甲方合作机构共享乙方的个人信息与信用信息，用于贷后管理与营销推荐。
""",
        ),
    ),
    # ---------------- 违约、罚息与催收 ----------------
    (
        "POS-14_BRCH-001_违约金40.txt",
        "consumer_loan",
        {"BRCH-001"},
        sample(
            "消费借款合同",
            "贷款人",
            "某某消费金融有限公司",
            CLEAN_LOAN
            + """
第四条 违约责任
乙方逾期还款的，应支付违约金，违约金为未还本金的 40%。
""",
        ),
    ),
    (
        "POS-15_BRCH-002_逾期利息与违约金并计.txt",
        "consumer_loan",
        {"BRCH-002"},
        sample(
            "消费借款合同",
            "贷款人",
            "某某消费金融有限公司",
            CLEAN_LOAN
            + """
第四条 逾期成本
乙方逾期的，逾期利息、违约金同时计收，并按笔加收催收费。
""",
        ),
    ),
    (
        "POS-16_BRCH-003_委托第三方催收.txt",
        "consumer_loan",
        {"BRCH-003"},
        sample(
            "消费借款合同",
            "贷款人",
            "某某消费金融有限公司",
            CLEAN_LOAN
            + """
第四条 逾期处理
乙方逾期超过 30 日的，甲方可委托第三方机构进行催收。
""",
        ),
    ),
    (
        "POS-17_BRCH-004_任意加速到期.txt",
        "consumer_loan",
        {"BRCH-004"},
        sample(
            "消费借款合同",
            "贷款人",
            "某某消费金融有限公司",
            CLEAN_LOAN
            + """
第四条 提前到期
乙方发生甲方认为影响其偿债能力的情形的，甲方有权宣布全部贷款提前到期。
""",
        ),
    ),
    (
        "POS-18_ROLL-001_借新还旧.txt",
        "consumer_loan",
        {"ROLL-001"},
        sample(
            "借款合同",
            "贷款人",
            "某某小额贷款有限公司",
            CLEAN_LOAN
            + """
第四条 续贷
借款到期未获清偿的，双方可办理自动续贷或借新还旧，原合同项下费用重新计收。
""",
        ),
    ),
    # ---------------- 担保与保证金 ----------------
    (
        "POS-19_GUAR-001_保证金30.txt",
        "guarantee",
        {"GUAR-001"},
        sample(
            "保证及抵押担保合同",
            "担保人",
            "平安融资担保有限公司",
            """第一条 担保安排
乙方委托甲方为其银行贷款提供连带责任保证担保，丙方以其名下房产提供抵押担保。

第二条 保证金
乙方应向甲方缴纳履约保证金，保证金比例为 30%，担保责任解除后 90 日内无息退还。

第三条 担保费
乙方按借款金额的 1% 向甲方支付担保费。
""",
            party_b="某商贸有限公司",
        ),
    ),
    (
        "POS-20_GUAR-002_连带责任未单独提示.txt",
        "guarantee",
        {"GUAR-002"},
        sample(
            "抵押担保合同",
            "担保人",
            "平安融资担保有限公司",
            """第一条 担保安排
乙方委托甲方为其借款提供担保，丙方以其名下车辆提供抵押担保。

第二条 责任范围
甲方承担连带责任保证；上述担保范围甲方未单独提示乙方，也未附于合同签署页。
""",
            party_b="某商贸有限公司",
        ),
    ),
    # ---------------- 争议解决与主体资格 ----------------
    (
        "POS-21_DISP-001_甲方所在地管辖.txt",
        "consumer_loan",
        {"DISP-001"},
        sample(
            "消费借款合同",
            "贷款人",
            "某某消费金融有限公司",
            CLEAN_LOAN
            + """
第四条 争议解决
因本合同发生的争议，由甲方所在地人民法院管辖。
""",
        ),
    ),
    (
        "POS-22_SUBJ-001_资金方以实际放款为准.txt",
        "partnership",
        {"SUBJ-001"},
        sample(
            "贷款推介与助贷服务协议",
            "服务方",
            "汇通信息科技有限公司",
            """鉴于甲方为金融机构提供借款导流与助贷服务，乙方通过甲方平台申请借款：

第一条 实际放款主体
本笔借款的资金方：以实际放款机构为准，甲方不在本协议中列明具体发放机构名称及其
金融许可证信息。

第二条 服务费用
甲方按借款金额的 2% 向乙方收取一次性服务费，该费用在放款前从乙方账户扣除。
""",
        ),
    ),
    # ---------------- 压力测试：单份合同同时命中多数红线 ----------------
    (
        "POS-23_ALL_全违规压力测试.txt",
        "consumer_loan",
        {
            "RATE-001", "RATE-003", "FEE-001", "FEE-002",
            "PROT-001", "PROT-002", "PROT-003", "PROT-004", "PROT-005", "INFO-001",
            "BRCH-001", "BRCH-002", "BRCH-003", "BRCH-004", "ROLL-001", "DISP-001",
            "SUBJ-001",
        },
        """高收益个人借款合同

甲方（贷款人）：贷款人：待补充
乙方（借款人）：某某

第一条 借款金额与放款
借款金额为 100,000 元。甲方在放款时预先扣除利息，实际放款金额为 92,000 元。

第二条 利率与费用
本合同月利率为 3%，借款期限 12 个月，每期还款金额 11,000 元。
除利息外，咨询服务费另行收取，平台管理费单独收取。

第三条 保险与授权
乙方必须投保借款人意外伤害保险，否则甲方有权拒绝放款。
乙方不可撤销地授权甲方从乙方任一账户直接扣划应付款项，无需另行通知。

第四条 逾期与违约
乙方逾期的，逾期利息、违约金同时计收，违约金为未还本金的 35%。
甲方有权宣布全部贷款提前到期，并可委托第三方机构进行催收。
乙方放弃对甲方计息方式的任何抗辩与异议。
借款到期未获清偿的，双方可办理借新还旧。

第五条 信息与管辖
乙方同意向甲方关联公司提供乙方的个人信息与信用信息。
甲方有权随时调整费率标准。本合同最终解释权归甲方所有。
因本合同发生的争议，由甲方所在地人民法院管辖。
""",
    ),
    # ---------------- 边界与回归守卫：曾因配置/正则缺陷而结果错误 ----------------
    (
        "EDGE-24_RATE-002_民间借贷12.2边界.txt",
        "private_lending",
        {"RATE-002"},
        sample(
            "民间借款合同",
            "出借人",
            "李某某",
            """第一条 借款金额
借款金额为 500,000 元。

第二条 利息
双方约定年利率为 12.2%。本利率参照合同成立时全国银行间同业拆借中心公布的
一年期贷款市场报价利率（LPR）的四倍上限协商确定。

第三条 还款
借款期限 12 个月，到期一次性归还本金及利息。
""",
            party_b="某贸易有限公司",
        ),
    ),
    # ---------------- 误报探针：记录当前正则的已知误报 ----------------
    (
        "EDGE-25_BRCH-002_否定表述不误报.txt",
        "consumer_loan",
        set(),
        sample(
            "消费借款合同",
            "贷款人",
            "某某消费金融有限公司",
            CLEAN_LOAN
            + """
第四条 逾期
乙方逾期的，甲方按未还本金每日万分之三计收罚息，罚息与违约金不重复计收。
""",
        ),
    ),
    # ---------------- 合规对照样本：期望零命中 ----------------
    (
        "NEG-01_合规消费贷.txt",
        "consumer_loan",
        set(),
        sample(
            "个人消费借款合同",
            "贷款人",
            "幸福消费金融股份有限公司",
            CLEAN_LOAN
            + """
第四条 逾期
乙方逾期的，甲方仅按未还本金每日万分之三计收罚息，不再另行主张违约金或滞纳金。

第五条 争议解决
因本合同发生的争议，由借款人住所地人民法院管辖。

第六条 个人信息
甲方仅在履行本合同所必需的范围内处理乙方个人信息，不向第三方提供。
""",
        ),
    ),
    (
        "NEG-02_合规民间借贷.txt",
        "private_lending",
        set(),
        sample(
            "民间借款合同",
            "出借人",
            "钱某某",
            """第一条 借款金额
借款金额为 100,000 元。

第二条 利息
双方约定年利率为 8%，未超过合同成立时一年期贷款市场报价利率（LPR）的四倍。

第三条 还款
借款期限 12 个月，到期一次性归还本金及利息。
""",
            party_b="某个体经营者",
        ),
    ),
    (
        "NEG-03_合规担保合同.txt",
        "guarantee",
        set(),
        sample(
            "保证及抵押担保合同",
            "担保人",
            "平安融资担保有限公司",
            """第一条 担保安排
乙方委托甲方为其银行贷款提供连带责任保证担保，丙方以其名下房产提供抵押担保。

第二条 保证金
乙方应向甲方缴纳履约保证金，保证金比例为 10%，担保责任解除后 10 个工作日内无息退还。

第三条 显著提示
甲方以加粗字体单独提示乙方及丙方注意担保范围、代偿触发条件与追偿路径，
并由乙方、丙方在签署页逐页签字确认。

第四条 担保费
乙方按借款金额的 0.5% 向甲方支付担保费，该费用已计入综合融资成本并以年化形式明示。
""",
            party_b="某商贸有限公司",
        ),
    ),
    (
        "NEG-04_合规助贷协议.txt",
        "partnership",
        set(),
        sample(
            "贷款推介与助贷服务协议",
            "服务方",
            "汇通信息科技有限公司",
            """鉴于甲方为金融机构提供借款导流与助贷服务，乙方通过甲方平台申请借款：

第一条 实际放款主体
本笔借款的资金方为兴业银行股份有限公司某某分行，金融许可证机构编码已在本协议
附件一中列明。甲方仅提供推介服务，不承担授信审批职责。

第二条 综合融资成本
本协议项下借款年利率为 10.8%，以年化形式明示，甲方不另行收取服务费或咨询费。

第三条 个人信息
乙方向资金方单独授权个人信息查询与使用，授权范围、目的与期限在本协议附件二列明。
""",
        ),
    ),
]


def build_engine() -> RuleEngine:
    from app.core.config import settings

    rows = as_rule_rows()
    for row in rows:
        row.setdefault("is_active", True)
    rules = [RedLineRule(**row) for row in rows]
    return RuleEngine(rules, settings)


def render_readme(
    matrix: list[tuple[str, str, list[str]]],
    all_codes: set[str],
    covered: set[str],
    rule_names: dict[str, str],
) -> str:
    """把实跑结果写成覆盖矩阵，避免 README 与正则漂移。"""
    lines = [
        "# 合同样本覆盖矩阵",
        "",
        "由 `scripts/gen_test_contracts.py` 实跑 `extract_metrics` + `RuleEngine` 后生成，",
        "手写文档迟早会和正则脱节，所以这份矩阵只认引擎的实际输出。",
        "",
        "- `POS-*` 应触发指定红线；`NEG-*` 一条都不应触发（测误报率）。",
        "- `EDGE-*` 是边界与回归守卫：这两条曾因 LPR 配置过期、BRCH-002 正则读不懂否定词而结果错误，",
        "  断言写的是**修复后的正确行为**，配置或正则再次跑偏时这里会先红。",
        "- 全部为 `.txt`，可直接上传或喂 `POST /api/v1/redlines/dry-run`（入参是纯文本）。",
        "",
        "| 样本 | 推断种类 | 实际命中红线 |",
        "| --- | --- | --- |",
    ]
    for filename, kind, codes in matrix:
        shown = "、".join(f"`{code}` {rule_names.get(code, '')}" for code in codes) or "（无命中）"
        lines.append(f"| `{filename}` | `{kind}` | {shown} |")

    lines += [
        "",
        f"## 红线覆盖：{len(covered)}/{len(all_codes)}",
        "",
        "| 编码 | 名称 | 是否被样本触发 |",
        "| --- | --- | --- |",
    ]
    for code in sorted(all_codes):
        mark = "✅" if code in covered else "❌ 未覆盖"
        lines.append(f"| `{code}` | {rule_names.get(code, '')} | {mark} |")

    lines += [
        "",
        "## 上传时的两个已知约束",
        "",
        "- `small_loan` 无法由 `infer_contract_kind` 推断出来（该函数没有对应分支），",
        "  测小贷规则集必须在上传时显式指定合同种类。",
        "- 只有 `.pdf / .docx / .txt / .md` 会被接受，上限 20 MB，且解析层没有 OCR：",
        "  扫描件会直接失败，需先自行转成带文字层的文件。",
        "",
    ]
    return "\n".join(lines)


def run(check_only: bool) -> int:
    engine = build_engine()
    all_codes = {row["code"] for row in as_rule_rows()}
    rule_names = {row["code"]: row["name"] for row in as_rule_rows()}
    covered: set[str] = set()
    failures: list[str] = []
    matrix: list[tuple[str, str, list[str]]] = []

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    for filename, expected_kind, expected_hits, text in SAMPLES:
        path = OUT_DIR / filename
        if check_only:
            if not path.exists():
                failures.append(f"{filename}: 文件不存在，请先不带 --check 运行一次")
                continue
            text = path.read_text(encoding="utf-8")
        else:
            path.write_text(text, encoding="utf-8")

        inferred = infer_contract_kind(text)
        doc: ExtractedDocument = build_document(text)
        segments = segmentize(doc)
        metrics = extract_metrics(doc, segments)
        evaluation = engine.evaluate(
            metrics, full_text=text, segments=segments, contract_kind=inferred
        )
        actual = {hit.rule_code for hit in evaluation.hits}
        covered |= actual

        if inferred != expected_kind:
            failures.append(f"{filename}: 种类推断为 {inferred}，期望 {expected_kind}")
        if actual != expected_hits:
            missing = sorted(expected_hits - actual)
            extra = sorted(actual - expected_hits)
            parts = []
            if missing:
                parts.append(f"未触发 {missing}")
            if extra:
                parts.append(f"意外触发 {extra}")
            failures.append(f"{filename}: {' / '.join(parts)}")

        unknown = sorted(actual - all_codes)
        if unknown:
            failures.append(f"{filename}: 引擎返回了未知编码 {unknown}")

        print(f"{'✓' if not (actual != expected_hits or inferred != expected_kind) else '✗'} "
              f"{filename:<52} {inferred:<16} {sorted(actual) or '（无命中）'}")
        matrix.append((filename, inferred, sorted(actual)))

    print(f"\n写入目录：{OUT_DIR}")
    print(f"样本 {len(SAMPLES)} 份，覆盖红线 {len(covered)}/{len(all_codes)}")
    gaps = sorted(all_codes - covered)
    if gaps:
        print(f"未覆盖红线：{gaps}")

    if not check_only:
        (OUT_DIR / "README.md").write_text(
            render_readme(matrix, all_codes, covered, rule_names), encoding="utf-8"
        )
        print(f"覆盖矩阵：{OUT_DIR / 'README.md'}")

    if failures:
        print(f"\n断言失败 {len(failures)} 项：")
        for item in failures:
            print(f"  - {item}")
        return 1
    print("\n全部断言通过。")
    return 0


if __name__ == "__main__":
    sys.exit(run("--check" in sys.argv))

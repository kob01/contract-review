"""把 `testdata/legal/` 的法规原文导入 `legal_provisions`（可复现的入库脚本）。

为什么这件事在评测体系里是**前置条件**而不是"知识库运营"：
库里 20 行条文有 19 行是 `【原文待导入】` 占位，引用核验一律返回未核验，
于是 GEN-06（引用已核验率）永远贴着地板、GEN-08（引用支持度）没法判、
RET-01（必达条文召回）连金标准主键都映射不出来。
**原文不入库，评测报告里"检索层"和"生成层"的分数是同一件事的两个影子。**

导入的两个坑，脚本都按口径处理了：

1. **法条级与法规级是两套条号**。10 条红线的 `basis_refs` 只到"整部法规"级
   （`article_no=None`），核验侧要求命中 `article_no='全文'` 那条哨兵行。
   所以除了逐条导入，必须把整篇正文也写进哨兵行，否则这些红线永远翻不成已核验。
2. **权威原文不许被占位覆盖**。`upsert_provision` 已有这条保护，本脚本沿用，
   因此可以反复跑；`--dry-run` 先看统计再决定写库。

用法：

    cd backend && PYTHONIOENCODING=utf-8 PYTHONPATH=. uv run python ../scripts/import_legal_corpus.py --dry-run
    ... import_legal_corpus.py                      # 只写 Postgres（不需要 embedding 服务）
    ... import_legal_corpus.py --vectorize          # 再同步 Qdrant 与 BM25（需 Ollama/ES 在线）
    ... import_legal_corpus.py --file 12_个人贷款管理办法.md   # 只导一部

`--vectorize` 单独开关是有意的：民法典 1260 条要 1260 次 embedding 调用，
把它和"入库"绑在一起会让一次误操作变成半小时的不可中断任务。
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent


def _setup_path() -> None:
    for candidate in (ROOT / "backend", ROOT / "scripts"):
        if str(candidate) not in sys.path:
            sys.path.insert(0, str(candidate))


_setup_path()

from app.core.logging import get_logger  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.models.enums import KnowledgeDocType  # noqa: E402
from app.services.rag.ingest import (  # noqa: E402
    LAW_LEVEL_ARTICLE_NO,
    ProvisionInput,
    referenced_provisions,
    upsert_provision,
)
from sqlalchemy import select  # noqa: E402

logger = get_logger(__name__)

CORPUS_DIR = ROOT / "testdata" / "legal"

#: 条号行首：第X条（中文数字与阿拉伯数字都收），后面允许全角或半角空格
ARTICLE_RE = re.compile(r"^(第[0-9零〇一二三四五六七八九十百千]+条)")
CHAPTER_RE = re.compile(r"^(第[0-9零〇一二三四五六七八九十百千]+[章节])")
PART_RE = re.compile(r"^(第[0-9零〇一二三四五六七八九十百千]+编)")

#: 文件名/法规名关键字 → 效力层级。顺序敏感：先匹配更具体的层级，
#: 否则"消费者权益保护法实施条例"会因为含"消费者权益保护法"被判成法律，
#: 而它是国务院行政法规（层级错了，检索过滤和展示口径都会跟着错）
DOC_TYPE_BY_HINT: tuple[tuple[str, str], ...] = (
    ("法释", KnowledgeDocType.JUDICIAL_INTERPRETATION.value),
    ("最高人民法院关于", KnowledgeDocType.JUDICIAL_INTERPRETATION.value),
    ("最高人民法院关于审理", KnowledgeDocType.JUDICIAL_INTERPRETATION.value),
    ("条例", KnowledgeDocType.ADMIN_REGULATION.value),
    ("金规", KnowledgeDocType.DEPARTMENT_RULE.value),
    ("银保监规", KnowledgeDocType.DEPARTMENT_RULE.value),
    ("银行保险机构", KnowledgeDocType.DEPARTMENT_RULE.value),
    ("管理办法", KnowledgeDocType.DEPARTMENT_RULE.value),
    ("暂行办法", KnowledgeDocType.DEPARTMENT_RULE.value),
    ("公告", KnowledgeDocType.REGULATORY_GUIDE.value),
    ("通知", KnowledgeDocType.REGULATORY_GUIDE.value),
    ("民法典", KnowledgeDocType.LAW.value),
    ("民事诉讼法", KnowledgeDocType.LAW.value),
    ("个人信息保护法", KnowledgeDocType.LAW.value),
    ("消费者权益保护法", KnowledgeDocType.LAW.value),
)


#: 法规名尾部的版本括注（"（2020年第二次修正）"）。必须剥掉：
#: 红线 `basis_refs` 引用的是法律的规范名称，核验按 `law_name` 精确匹配，
#: 带括注入库会让每一条引用都判成"知识库未收录"——名字对不上，条文再全也没用。
#: 版本信息不丢：它进 `version_label`，报告页仍可展示"据 2020 年第二次修正文本"
VERSION_SUFFIX_RE = re.compile(r"(（[^（）]*(?:修正|修订|试行|草案)[^（）]*）)$")


def canonical_law_name(raw: str) -> str:
    """取规范名称；括注里的版本说明留给 version_label。"""
    return VERSION_SUFFIX_RE.sub("", raw.strip()).strip()


def law_title_of(path: Path) -> str:
    """H1 原始标题（带版本括注）。"""
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return path.stem.split("_", 1)[-1]


def law_name_of(path: Path) -> str:
    """取正文 H1 的法规名，而不是文件名（文件名带序号前缀）。"""
    return canonical_law_name(law_title_of(path))


def doc_type_of(name: str) -> str:
    for hint, doc_type in DOC_TYPE_BY_HINT:
        if hint in name:
            return doc_type
    return KnowledgeDocType.DEPARTMENT_RULE.value


def parse_metadata(text: str) -> dict[str, str]:
    """解析文件开头的 `| 项目 | 内容 |` 元信息表。"""
    meta: dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip().startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) != 2 or cells[0] in {"项目", "---", ""} or set(cells[1]) <= {"-"}:
            continue
        meta.setdefault(cells[0], cells[1])
        if len(meta) >= 12:
            break
    return meta


def parse_date(value: str | None) -> date | None:
    if not value:
        return None
    matched = re.search(r"(\d{4})[-年](\d{1,2})[-月](\d{1,2})", value)
    if not matched:
        return None
    year, month, day = (int(part) for part in matched.groups())
    try:
        return date(year, month, day)
    except ValueError:
        return None


CJK_NUMBER_RE = re.compile(r"^([一二三四五六七八九十百]+)、")

#: 正文短于这个字数就认为文件只是元信息占位（比如民诉法那份还没抓到正文），
#: 不生成任何行——生成一条空的"全文"行会把引用伪装成已核验，比缺行更坏
MIN_BODY_CHARS = 200

#: 列宽（与 models/knowledge.py 一致）。入库前逐个裁剪：
#: 抓取来的元信息字段常带整段说明（"法释〔2020〕17号（该号为第二次修正所依据的…）"），
#: 直接写会撞 varchar 长度上限，而报错点在一千多条里的某一条上最难查
COLUMN_LIMITS: dict[str, int] = {
    "doc_type": 32,
    "law_name": 255,
    "article_no": 64,
    "chapter": 128,
    "title": 255,
    "issuing_body": 255,
    "version_label": 64,
    "source_url": 1024,
}

_DOC_NO_RE = re.compile(r"([a-zA-Z]*〔\d{4}〕\s*\d+\s*号|第\d+号)")


def _clip(field: str, value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    limit = COLUMN_LIMITS.get(field, 255)
    return text if len(text) <= limit else text[: max(0, limit - 1)] + "…"


def _normalize_version_label(raw: str | None) -> str | None:
    """文号只取编号本身（如"法释〔2020〕17号"），后面的说明性长句丢掉。"""
    if not raw:
        return None
    hit = _DOC_NO_RE.search(raw)
    return _clip("version_label", hit.group(1).replace(" ", "") if hit else raw)


def _normalize_source_url(raw: str | None) -> str | None:
    """多个来源交叉校验时只保留第一个 URL，其余进不了 1024 字符的列宽。"""
    if not raw:
        return None
    first = re.split(r"[；;]", raw)[0].strip()
    return _clip("source_url", first)


def split_articles(body: str) -> list[tuple[str, str, str]]:
    """把正文切成 `[(条号, 章, 条文原文)]`。

    条号之前的所有内容（标题、目录、通过日期）不进任何条文——它是"这部法"的内容，
    归到第一条会让第一条的语义被污染，检索时把目录当成条文召回。
    目录段（"第一编 总则"这类连续短行）也跳过，否则 1260 条的目录会被切成一堆伪条文。
    """
    articles: list[tuple[str, str, str]] = []
    current_no: str | None = None
    chapter = ""
    buffer: list[str] = []
    in_toc = False

    def flush() -> None:
        if current_no is None:
            return
        content = "\n".join(buffer).strip()
        if content:
            articles.append((current_no, chapter, content))

    for raw_line in body.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped:
            if current_no is not None:
                buffer.append("")
            continue

        article_hit = ARTICLE_RE.match(stripped)
        if article_hit:
            flush()
            current_no = article_hit.group(1)
            buffer = [stripped]
            in_toc = False
            continue

        part_hit = PART_RE.match(stripped)
        chapter_hit = CHAPTER_RE.match(stripped)
        if part_hit or chapter_hit:
            # 遇到新的编/章而还没进入任何条文，说明还在目录区
            if current_no is None:
                in_toc = True
                continue
            in_toc = False
            chapter = stripped
            if current_no is not None:
                buffer.append(stripped)
            continue

        if current_no is None or in_toc:
            continue
        buffer.append(stripped)

    flush()
    return articles


def split_numbered_items(body: str) -> list[tuple[str, str, str]]:
    """按"一、二、三、"切分公告/通知/批复类文件。

    监管公告与司法批复大多不用"第X条"，而是用中文序号分段（人行 2021 年第 3 号公告、
    银保监规 2022 年 14 号通知都是这个形状）。不认这种编号，这些法规在库里就是空的，
    红线引用的依据永远核不上。条号沿用原文序号，与人工引用时的写法保持一致。
    """
    items: list[tuple[str, str, str]] = []
    current: str | None = None
    buffer: list[str] = []

    for raw_line in body.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            if current is not None:
                buffer.append("")
            continue
        hit = CJK_NUMBER_RE.match(stripped)
        if hit:
            if current is not None and buffer:
                items.append((current, "", "\n".join(buffer).strip()))
            current = f"{hit.group(1)}、"
            buffer = [stripped]
            continue
        if current is None:
            continue
        buffer.append(stripped)

    if current is not None and buffer:
        items.append((current, "", "\n".join(buffer).strip()))
    return items


def build_inputs(path: Path) -> tuple[list[ProvisionInput], str]:
    """返回 (待写行, 说明)。说明用于报告"这部为什么是空的"。"""
    text = path.read_text(encoding="utf-8")
    name = law_name_of(path)
    doc_type = doc_type_of(name)
    meta = parse_metadata(text)

    issuing_body = meta.get("通过/修正机关与会议") or meta.get("发布机关")
    version_label = meta.get("文号")
    if not version_label:
        # H1 里的版本括注是唯一的版本线索，别丢
        hit = VERSION_SUFFIX_RE.search(law_title_of(path) or "")
        version_label = hit.group(1) if hit else None
    effective_on = parse_date(meta.get("施行日期"))
    status = meta.get("效力状态") or ""
    repealed_on = parse_date(meta.get("废止日期")) if "废止" in status else None
    source_url = meta.get("来源URL")

    body = text.split("\n---", 1)[-1] if "\n---" in text else text
    articles = split_articles(body)
    splitter = "第X条"
    if not articles:
        articles = split_numbered_items(body)
        splitter = "一、序号" if articles else "无"

    common: dict[str, Any] = {
        "doc_type": _clip("doc_type", doc_type),
        "issuing_body": _clip("issuing_body", issuing_body),
        "effective_on": effective_on,
        "repealed_on": repealed_on,
        "version_label": _normalize_version_label(version_label),
        "source_url": _normalize_source_url(source_url),
    }

    if not articles and len(body.strip()) < MIN_BODY_CHARS:
        # 只有元信息、没有正文的文件（民诉法那份就是如此）一律不写：
        # 写一条空的"全文"行会让引用核验把它当成权威原文，比库里没这部法更危险
        return [], f"正文仅 {len(body.strip())} 字，视为未抓到原文，跳过"

    inputs: list[ProvisionInput] = []
    for article_no, chapter, content in articles:
        inputs.append(
            ProvisionInput(
                law_name=_clip("law_name", name),  # type: ignore[arg-type]
                article_no=_clip("article_no", article_no),  # type: ignore[arg-type]
                content=content,
                chapter=_clip("chapter", chapter) or None,
                **common,
            )
        )

    # 法规级哨兵行：basis_refs 只到"整部法规"的红线，核验只认这一条
    # （见 services/citation.py 的 _lookup：article_no 为空时按 LAW_LEVEL_ARTICLE_NO 查）
    law_level_needed = any(
        ref_name == name and ref_article is None for ref_name, ref_article, _ in referenced_provisions()
    )
    if law_level_needed:
        fulltext = "\n".join(f"{no}　{content}" for no, _chapter, content in articles) or body.strip()
        inputs.append(
            ProvisionInput(
                law_name=_clip("law_name", name),  # type: ignore[arg-type]
                article_no=LAW_LEVEL_ARTICLE_NO,
                # 整篇正文存一行：核验只要求"这一行存在且有权威内容"，
                # 逐条检索仍走上面的条文行，两者不互相替代
                content=fulltext,
                **common,
            )
        )
    return inputs, f"doc_type={doc_type} 切分={splitter} 哨兵行={'需要' if law_level_needed else '不需要'}"


async def prune_suffixed_duplicates() -> int:
    """删掉带版本括注的重复法规行。

    规范名称才是红线引用的键，带括注的那批是历史/首次导入留下的副本；
    留着它们不会让核验失败，但会让检索结果里同一部法出现两次、
    并让"法规部数"这类统计虚高。删除前打印清单，删除只针对确有规范行的那些括注行。
    """
    from app.models.knowledge import LegalProvision

    removed = 0
    async with SessionLocal() as session:
        rows = (await session.execute(select(LegalProvision))).scalars().all()
        canonical = {canonical_law_name(row.law_name) for row in rows}
        for row in rows:
            if row.law_name == canonical_law_name(row.law_name):
                continue
            if row.law_name in canonical:
                continue
            if canonical_law_name(row.law_name) not in canonical:
                continue
            print(f"  删除重复：{row.law_name} {row.article_no}")
            await session.delete(row)
            removed += 1
        await session.commit()
    print(f"清理重复法规行 {removed} 行")
    return removed


async def import_corpus(files: list[Path], *, dry_run: bool, vectorize: bool) -> int:
    total_created = total_updated = 0
    report: list[tuple[str, int, int, str]] = []

    for path in files:
        inputs, note = build_inputs(path)
        if not inputs:
            report.append((path.name, 0, 0, note))
            continue
        if dry_run:
            report.append((path.name, len(inputs), 0, note))
            continue

        async with SessionLocal() as session:
            for payload in inputs:
                _row, is_new = await upsert_provision(session, payload)
                total_created += 1 if is_new else 0
                total_updated += 0 if is_new else 1
            await session.commit()
        report.append((path.name, len(inputs), 0, "已写入 Postgres"))

    print(f"\n{'预演' if dry_run else '导入'}：{len(files)} 部法规")
    for name, count, _updated, note in report:
        print(f"  · {name:<62} 条文 {count:>5}  {note}")
    if not dry_run:
        print(f"\n新建 {total_created} 行，更新 {total_updated} 行")
        if vectorize:
            print("开始同步 Qdrant 与 BM25（需要 embedding 服务与 ES 在线）…")
            from app.services.rag.ingest import sync_sparse_index, sync_vector_store

            async with SessionLocal() as session:
                vectors = await sync_vector_store(session)
                sparse = await sync_sparse_index(session)
            print(f"向量 {vectors} 条，BM25 {sparse} 条")
        else:
            print("未做向量化：逐条召回的 vector/BM25 通道还看不见这些条文，"
                  "exact_ref 直连与关键词通道已经可用。跑 --vectorize 补齐另两路")
    return 0


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="法条原文入库")
    parser.add_argument("--dry-run", action="store_true", help="只统计不写库")
    parser.add_argument("--vectorize", action="store_true", help="导入后同步 Qdrant 与 BM25")
    parser.add_argument("--file", default="", help="只导入文件名包含该串的一部法规")
    parser.add_argument(
        "--prune-suffixed",
        action="store_true",
        help="删除 law_name 带版本括注且规范行已存在的重复条文",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.prune_suffixed:
        asyncio.run(prune_suffixed_duplicates())
        return 0
    if not CORPUS_DIR.exists():
        print(f"语料目录不存在：{CORPUS_DIR}")
        return 2
    files = sorted(CORPUS_DIR.glob("*.md"))
    if args.file:
        files = [path for path in files if args.file in path.name]
    if not files:
        print("没有匹配的语料文件")
        return 2
    return asyncio.run(import_corpus(files, dry_run=args.dry_run, vectorize=args.vectorize))


if __name__ == "__main__":
    sys.exit(main())

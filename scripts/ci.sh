#!/usr/bin/env bash
# 本地一键评测（与 .github/workflows/eval.yml 的 rule-only 阶段同序）。
#
#   bash scripts/ci.sh              # 静态检查 + 规则通道回归 + 门禁
#   STAGE=full bash scripts/ci.sh   # 再跑一遍真实模型全链路（会产生 token 费用）
#
# 为什么要在本地也留一份和 CI 同序的脚本：CI 上第一次才暴露的问题，
# 修复成本比本地高一个量级，而且开发者会开始跳过 CI。
set -uo pipefail

cd "$(dirname "$0")/../backend" || exit 2
export PYTHONIOENCODING=utf-8
export PYTHONPATH=.

failed=0
step() {
  local name="$1"; shift
  echo
  echo "=== $name ==="
  if "$@"; then
    echo "  ✓ $name"
  else
    echo "  ✗ $name（退出码 $?）"
    failed=1
  fi
}

step "ruff" uv run ruff check app tests ../scripts
step "单元测试" uv run pytest -q
step "夹具与规则引擎一致性" uv run python ../scripts/gen_test_contracts.py --check
step "金标准与引擎一致性" uv run python ../scripts/gen_gold.py --check
step "配置漂移审计" uv run python ../scripts/audit_config_drift.py --only rules
step "法条原文入库" uv run python ../scripts/import_legal_corpus.py
step "评测（规则通道）" env LLM_PROVIDER=mock RAG_USE_MOCK_EMBEDDING=true QUEUE_ENABLED=false \
  RAG_SPARSE_ENABLED=false RAG_RERANK_ENABLED=false \
  uv run python ../scripts/eval.py --rule-only
step "门禁（rule-only 阶段）" uv run python ../scripts/gate.py --stage rule-only

if [ "${STAGE:-rule-only}" = "full" ]; then
  echo
  echo "=== 评测（完整链路：真实模型，会产生费用）==="
  uv run python ../scripts/eval.py --repeats "${REPEATS:-1}" --set-baseline && \
    uv run python ../scripts/gate.py --stage full || failed=1
fi

echo
if [ "$failed" -eq 0 ]; then
  echo "全部通过"
else
  echo "有步骤未通过，见上方 ✗"
fi
exit $failed

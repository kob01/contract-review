"""测试数据。

样本合同覆盖：日息万分之五、月利率 3 分、按月收服务费、砍头息、
单方调整费率、放弃抗辩、最终解释权、搭售保险、管辖偏向等典型风险表述。
"""

from __future__ import annotations

from app.services.parser.extractor import ExtractedDocument, PageText

CONTRACT_TEXT = """个人消费借款合同

甲方（出借人）：某某小额贷款有限公司
乙方（借款人）：张三

第一条 借款金额与放款
借款金额为 100,000 元。甲方于审批通过后一次性放款，实际放款金额为 95,000 元，
差额部分为咨询服务费，从本金中预先扣除。

第二条 利率与费用
本合同日利率为万分之五。乙方另需按月承担服务费用，每月按借款本金的 1% 收取担保费。
综合年化融资成本不超过 36%。

第三条 还款方式
借款期限 12 个月，每期还款金额 10,000 元，等额本息偿还。

第四条 逾期与违约
乙方未按期还款的，甲方有权按未还本金每日千分之一计收罚息，并可宣布全部贷款提前到期。
乙方放弃对甲方催收行为的任何抗辩与异议权利。

第五条 保险与授权
乙方必须投保借款人意外伤害保险，否则甲方有权拒绝放款。
乙方不可撤销地授权甲方从乙方任一账户直接扣划应付款项，无需另行通知。

第六条 其他
本合同最终解释权归甲方所有。甲方有权单方调整服务费率标准。
因本合同产生的争议由甲方所在地人民法院管辖。
"""


def build_document(text: str = CONTRACT_TEXT, pages: int = 1) -> ExtractedDocument:
    """构造单页/多页抽取结果，模拟 PDF 分页拼接。"""
    if pages <= 1:
        return ExtractedDocument(text=text, pages=[PageText(number=1, text=text, char_start=0)])

    size = len(text) // pages
    page_texts = []
    cursor = 0
    for number in range(1, pages + 1):
        chunk = text[cursor : cursor + size] if number < pages else text[cursor:]
        page_texts.append(PageText(number=number, text=chunk, char_start=cursor))
        cursor += len(chunk)
    return ExtractedDocument(text=text, pages=page_texts)

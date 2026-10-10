import type {
  ContractDetail,
  ContractListResponse,
  HealthResponse,
  HumanReviewTask,
  LegalProvision,
  RedLineRule,
  ReviewReport,
  ReviewRunRequest,
  ReviewSummary,
  RuleDryRunResponse,
  ThresholdSnapshot,
} from "@/types";
import { http } from "./client";

export const getHealth = async (): Promise<HealthResponse> =>
  (await http.get<HealthResponse>("/health")).data;

// ---------- 合同 ----------

export const listContracts = async (params?: {
  status?: string;
  contractKind?: string;
  keyword?: string;
  page?: number;
}): Promise<ContractListResponse> =>
  (
    await http.get<ContractListResponse>("/contracts", {
      params: {
        page_size: 50,
        status: params?.status,
        contract_kind: params?.contractKind,
        keyword: params?.keyword,
        page: params?.page,
      },
    })
  ).data;

export const getContract = async (id: string): Promise<ContractDetail> =>
  (await http.get<ContractDetail>(`/contracts/${id}`)).data;

export const uploadContract = async (
  file: File,
  form: {
    title?: string;
    contractKind?: string;
    lenderName?: string;
    borrowerName?: string;
  },
): Promise<ContractDetail> => {
  const payload = new FormData();
  payload.append("file", file);
  if (form.title) payload.append("title", form.title);
  if (form.contractKind) payload.append("contract_kind", form.contractKind);
  if (form.lenderName) payload.append("lender_name", form.lenderName);
  if (form.borrowerName) payload.append("borrower_name", form.borrowerName);
  return (await http.post<ContractDetail>("/contracts", payload)).data;
};

export const reparseContract = async (id: string): Promise<ContractDetail> =>
  (await http.post<ContractDetail>(`/contracts/${id}/reparse`)).data;

export const deleteContract = async (id: string): Promise<void> => {
  await http.delete(`/contracts/${id}`);
};

/** PDF.js 直接取原始文件流。 */
export const contractFileUrl = (id: string): string =>
  `${http.defaults.baseURL}/contracts/${id}/file`;

// ---------- 审查 ----------

export const startReview = async (
  contractId: string,
  request: ReviewRunRequest = {},
): Promise<ReviewSummary> =>
  (await http.post<ReviewSummary>(`/contracts/${contractId}/reviews`, request))
    .data;

export const getReview = async (id: string): Promise<ReviewSummary> =>
  (await http.get<ReviewSummary>(`/reviews/${id}`)).data;

export const getReport = async (id: string): Promise<ReviewReport> =>
  (await http.get<ReviewReport>(`/reviews/${id}/report`)).data;

export const listContractReviews = async (
  contractId: string,
): Promise<ReviewSummary[]> =>
  (
    await http.get<{ items: ReviewSummary[] }>(
      `/contracts/${contractId}/reviews`,
    )
  ).data.items;

// ---------- 人工复核 ----------

export const listReviewQueue = async (params?: {
  status?: string;
  contractId?: string;
}): Promise<HumanReviewTask[]> =>
  (
    await http.get<{ items: HumanReviewTask[] }>("/human-review/queue", {
      params: {
        status: params?.status ?? "pending",
        contract_id: params?.contractId,
      },
    })
  ).data.items;

export const resolveReviewTask = async (
  taskId: string,
  payload: {
    verdict: "confirmed" | "false_positive" | "need_adjustment";
    reviewerId?: string;
    comment?: string;
    adjustedRiskLevel?: string;
  },
): Promise<HumanReviewTask> =>
  (
    await http.post<HumanReviewTask>(`/human-review/tasks/${taskId}/resolve`, {
      verdict: payload.verdict,
      reviewer_id: payload.reviewerId ?? "legal",
      comment: payload.comment ?? "",
      adjusted_risk_level: payload.adjustedRiskLevel,
    })
  ).data;

export const reviewStats = async (): Promise<Record<string, number | null>> =>
  (await http.get("/human-review/stats")).data;

// ---------- 红线规则 ----------

export const listRules = async (): Promise<RedLineRule[]> =>
  (await http.get<RedLineRule[]>("/redlines")).data;

export const getThresholds = async (): Promise<ThresholdSnapshot> =>
  (await http.get<ThresholdSnapshot>("/redlines/thresholds")).data;

export const dryRunRules = async (
  text: string,
  contractKind = "consumer_loan",
): Promise<RuleDryRunResponse> =>
  (
    await http.post<RuleDryRunResponse>("/redlines/dry-run", {
      text,
      contract_kind: contractKind,
    })
  ).data;

export const toggleRule = async (
  code: string,
  isActive: boolean,
): Promise<RedLineRule> =>
  (
    await http.post<RedLineRule>(`/redlines/${code}/toggle`, {
      is_active: isActive,
    })
  ).data;

// ---------- 知识库 ----------

export const searchProvisions = async (
  query: string,
): Promise<{
  items: LegalProvision[];
  matchKinds: string[];
  embeddingIsMock: boolean;
}> => {
  const data = (
    await http.get("/knowledge/search", { params: { q: query, top_k: 8 } })
  ).data;
  return {
    items: data.items,
    matchKinds: data.match_kinds ?? data.matchKinds ?? [],
    embeddingIsMock: data.embedding_is_mock ?? data.embeddingIsMock ?? false,
  };
};

export const listProvisions = async (params?: {
  lawName?: string;
  onlyUnimported?: boolean;
}): Promise<LegalProvision[]> =>
  (
    await http.get<LegalProvision[]>("/knowledge/provisions", {
      // 查询参名以后端定义为准（snake_case）
      params: {
        law_name: params?.lawName,
        only_unimported: params?.onlyUnimported,
      },
    })
  ).data;

export const importProvision = async (payload: {
  lawName: string;
  articleNo: string;
  content: string;
  docType?: string;
  sourceUrl?: string;
}): Promise<LegalProvision> =>
  (
    await http.post<LegalProvision>("/knowledge/provisions", {
      law_name: payload.lawName,
      article_no: payload.articleNo,
      content: payload.content,
      doc_type: payload.docType ?? "department_rule",
      source_url: payload.sourceUrl,
    })
  ).data;

export const knowledgeStats = async (): Promise<
  Record<string, number | boolean | string>
> => (await http.get("/knowledge/stats")).data;

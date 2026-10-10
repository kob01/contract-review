import { lazy, Suspense } from "react";
import { Route, Routes } from "react-router-dom";
import AppShell from "@/components/AppShell";
import ContractListPage from "@/pages/ContractListPage";
import UploadPage from "@/pages/UploadPage";
import ReviewQueuePage from "@/pages/ReviewQueuePage";
import RulesPage from "@/pages/RulesPage";
import KnowledgePage from "@/pages/KnowledgePage";
import NotFoundPage from "@/pages/NotFoundPage";

// PDF.js 体积很大，只在真正打开报告时才拉取对应 chunk
const ReviewReportPage = lazy(() => import("@/pages/ReviewReportPage"));

const Fallback = () => (
  <div className="py-16 text-center text-sm text-slate-500">
    正在加载审查报告…
  </div>
);

export default function App() {
  return (
    <Routes>
      <Route element={<AppShell />}>
        <Route index element={<ContractListPage />} />
        <Route path="upload" element={<UploadPage />} />
        <Route
          path="contracts/:contractId/reviews/:reviewId"
          element={
            <Suspense fallback={<Fallback />}>
              <ReviewReportPage />
            </Suspense>
          }
        />
        <Route path="queue" element={<ReviewQueuePage />} />
        <Route path="rules" element={<RulesPage />} />
        <Route path="knowledge" element={<KnowledgePage />} />
        <Route path="*" element={<NotFoundPage />} />
      </Route>
    </Routes>
  );
}

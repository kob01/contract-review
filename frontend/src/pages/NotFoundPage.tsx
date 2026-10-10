import { Link } from "react-router-dom";

export default function NotFoundPage() {
  return (
    <div className="py-20 text-center">
      <h1 className="text-2xl font-semibold text-slate-900">页面不存在</h1>
      <p className="mt-2 text-sm text-slate-500">
        链接可能来自一次已被删除的审查。
      </p>
      <Link
        to="/"
        className="mt-4 inline-block rounded-lg bg-slate-900 px-4 py-2 text-sm text-white hover:bg-slate-700"
      >
        返回合同列表
      </Link>
    </div>
  );
}

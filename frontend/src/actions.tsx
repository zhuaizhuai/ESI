import { useEffect, useState } from "react";
import { api } from "./session";
import type { User } from "./session";
import { EvidenceValue } from "./capabilities";
export type ActionItem = {
  id: string;
  title: string;
  action: string;
  status: string;
  assignee_id?: string;
  assignee_name?: string;
  success_metric: string;
  outcome: string;
  due_at?: number | null;
  overdue?: boolean;
  metric_id?: string | null;
  baseline?: number | null;
  target?: number | null;
  direction?: string;
  start_date?: string | null;
  end_date?: string | null;
  dimensions?: Record<string, unknown>;
  measurement?: unknown;
  development_task_id?: string;
  can_configure?: boolean;
  can_measure?: boolean;
  can_development?: boolean;
  can_feedback?: boolean;
  history?: {
    action: string;
    detail: Record<string, unknown>;
    created: number;
  }[];
};
type Person = { id: string; display_name: string };
type Project = { id: string; name: string; my_role: string };
type Metric = { id: string; name: string; source_name: string };
type ApprovalItem = {
  kind: "development" | "analysis";
  job_id: string;
  title: string;
  version: number;
  resource_id: string;
  name: string;
  can_approve: boolean;
  scoped_plan?: unknown;
};
type AcceptanceItem = {
  kind: "development" | "analysis";
  job_id: string;
  title: string;
  version: number;
  resource_id: string;
  name: string;
  can_accept: boolean;
  scoped_result?: unknown;
};
const states: Record<string, string> = {
  proposed: "待采纳",
  accepted: "已采纳",
  in_progress: "执行中",
  done: "已完成",
  dismissed: "不采纳",
};
const dateInput = (value?: number | null) => {
  if (!value) return "";
  const date = new Date(value * 1000);
  return new Date(date.getTime() - date.getTimezoneOffset() * 60000)
    .toISOString()
    .slice(0, 16);
};
export function ActionCard({
  item,
  user,
  canManage = false,
  onChanged,
}: {
  item: ActionItem;
  user: User;
  canManage?: boolean;
  onChanged: () => Promise<void>;
}) {
  const [error, setError] = useState(""),
    [busy, setBusy] = useState(false),
    [mode, setMode] = useState(""),
    [people, setPeople] = useState<Person[]>([]),
    [projects, setProjects] = useState<Project[]>([]),
    [metrics, setMetrics] = useState<Metric[]>([]);
  const manage = item.can_configure ?? canManage;
  useEffect(() => {
    if (!manage) return;
    api("/directory")
      .then(setPeople)
      .catch((e) => setError(e.message));
    api("/projects")
      .then(setProjects)
      .catch((e) => setError(e.message));
    api("/data-sources")
      .then((sources) =>
        setMetrics(
          sources.flatMap(
            (s: { name: string; metrics: { id: string; name: string }[] }) =>
              s.metrics.map((m) => ({ ...m, source_name: s.name })),
          ),
        ),
      )
      .catch((e) => setError(e.message));
  }, [manage]);
  const act = async (path: string, body: unknown) => {
    setBusy(true);
    setError("");
    try {
      await api(path, body);
      await onChanged();
      setMode("");
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  return (
    <article className="recommendation">
      <div className="section-heading">
        <h3>{item.title}</h3>
        <span className={`badge ${item.status === "done" ? "completed" : ""}`}>
          {states[item.status] || item.status}
        </span>
      </div>
      <p className="preserve">{item.action}</p>
      <p className="hint">
        负责人：{item.assignee_name || "待分配"} · 衡量指标：
        {item.success_metric || "待明确"}
      </p>
      <p className={item.overdue ? "warning-note" : "hint"}>
        {item.due_at
          ? `截止：${new Date(item.due_at * 1000).toLocaleString()}${item.overdue ? " · 已逾期" : ""}`
          : "尚未设置截止日期"}
        {item.baseline !== null && item.baseline !== undefined
          ? ` · 基线：${item.baseline}`
          : ""}
        {item.target !== null && item.target !== undefined
          ? ` · 目标：${item.target}`
          : ""}
      </p>
      {item.outcome && <p className="preserve">执行反馈：{item.outcome}</p>}
      {item.development_task_id && (
        <p className="hint">已转研发任务：{item.development_task_id}</p>
      )}
      {item.measurement != null && (
        <details>
          <summary>效果复测结果</summary>
          <EvidenceValue value={item.measurement} />
          <p className="hint">前后指标变化用于评估效果，不直接证明因果关系。</p>
        </details>
      )}
      {item.history?.length ? (
        <details>
          <summary>反馈历史</summary>
          {item.history.map((h, i) => (
            <div key={i}>
              {new Date(h.created * 1000).toLocaleString()} ·{" "}
              {(
                {
                  feedback: "进展反馈",
                  configured: "分配更新",
                  measured: "效果复测",
                  development: "转研发",
                } as Record<string, string>
              )[h.action] || h.action}
              <EvidenceValue value={h.detail} />
            </div>
          ))}
        </details>
      ) : null}
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      <div className="actions">
        {manage && (
          <button
            disabled={busy}
            onClick={() => setMode(mode === "configure" ? "" : "configure")}
          >
            分配与衡量目标
          </button>
        )}
        {(item.can_measure ?? canManage) && item.metric_id && (
          <button
            disabled={busy}
            onClick={() => act(`/actions/${item.id}/measure`, {})}
          >
            {busy ? "处理中…" : "复测指标"}
          </button>
        )}
        {(item.can_development ?? canManage) && !item.development_task_id && (
          <button
            disabled={busy}
            onClick={() => setMode(mode === "development" ? "" : "development")}
          >
            转为研发任务
          </button>
        )}
      </div>
      {mode === "configure" && manage && (
        <form
          className="action-form"
          onSubmit={(e) => {
            e.preventDefault();
            const f = new FormData(e.currentTarget);
            const due = String(f.get("due_at") || "");
            try {
              const dimensions = JSON.parse(
                String(f.get("dimensions") || "{}"),
              );
              if (
                !dimensions ||
                typeof dimensions !== "object" ||
                Array.isArray(dimensions)
              )
                throw new Error("筛选维度必须是对象");
              act(`/actions/${item.id}/configure`, {
                assignee_id: f.get("assignee_id"),
                due_at: due ? Math.floor(new Date(due).getTime() / 1000) : null,
                metric_id: f.get("metric_id") || null,
                baseline:
                  f.get("baseline") === "" ? null : Number(f.get("baseline")),
                target: f.get("target") === "" ? null : Number(f.get("target")),
                direction: f.get("direction"),
                start_date: f.get("start_date") || null,
                end_date: f.get("end_date") || null,
                dimensions,
              });
            } catch (e) {
              setError((e as Error).message);
            }
          }}
        >
          <div className="field-grid">
            <label>
              负责人
              <select
                name="assignee_id"
                required
                defaultValue={item.assignee_id || ""}
              >
                <option value="" disabled>
                  选择员工
                </option>
                {people.map((p) => (
                  <option value={p.id} key={p.id}>
                    {p.display_name}
                  </option>
                ))}
              </select>
            </label>
            <label>
              截止日期（本地时间）
              <input
                type="datetime-local"
                name="due_at"
                defaultValue={dateInput(item.due_at)}
              />
            </label>
            <label>
              复测指标
              <select name="metric_id" defaultValue={item.metric_id || ""}>
                <option value="">暂不配置</option>
                {metrics.map((m) => (
                  <option key={m.id} value={m.id}>
                    {m.source_name} · {m.name}
                  </option>
                ))}
              </select>
            </label>
            <label>
              期望方向
              <select
                name="direction"
                defaultValue={item.direction || "increase"}
              >
                <option value="increase">提高</option>
                <option value="decrease">降低</option>
              </select>
            </label>
            <label>
              基线
              <input
                type="number"
                step="any"
                name="baseline"
                defaultValue={item.baseline ?? ""}
              />
            </label>
            <label>
              目标
              <input
                type="number"
                step="any"
                name="target"
                defaultValue={item.target ?? ""}
              />
            </label>
            <label>
              复测开始日期
              <input
                type="date"
                name="start_date"
                defaultValue={item.start_date || ""}
              />
            </label>
            <label>
              复测结束日期
              <input
                type="date"
                name="end_date"
                defaultValue={item.end_date || ""}
              />
            </label>
          </div>
          <p className="hint">
            仅配置了日期字段的指标支持固定时间范围；请使用与基线可比较的统计范围。
          </p>
          <details>
            <summary>限定复测范围</summary>
            <label>
              维度筛选
              <textarea
                name="dimensions"
                defaultValue={JSON.stringify(item.dimensions || {}, null, 2)}
                rows={3}
              />
            </label>
            <p className="hint">
              例如 {`{"region":"华东"}`}，不填则使用整体指标。
            </p>
          </details>
          <button className="primary" disabled={busy}>
            保存分配与目标
          </button>
          <button type="button" onClick={() => setMode("")}>
            取消
          </button>
        </form>
      )}
      {mode === "development" && (
        <form
          className="action-form"
          onSubmit={(e) => {
            e.preventDefault();
            const f = new FormData(e.currentTarget);
            act(`/actions/${item.id}/development`, {
              project_ids: f.getAll("project_ids"),
              title: f.get("title"),
              requirement: f.get("requirement"),
              acceptance: f.get("acceptance"),
            });
          }}
        >
          <label>
            任务标题
            <input
              name="title"
              required
              maxLength={200}
              defaultValue={item.title}
            />
          </label>
          <fieldset className="source-picker">
            <legend>实施仓库</legend>
            {projects
              .filter((p) => p.my_role !== "viewer")
              .map((p) => (
                <label key={p.id}>
                  <input type="checkbox" name="project_ids" value={p.id} />
                  {p.name}
                </label>
              ))}
            {!projects.some((p) => p.my_role !== "viewer") && (
              <p className="hint">
                暂无可创建任务的仓库，请联系项目负责人授权。
              </p>
            )}
          </fieldset>
          <label>
            需求说明
            <textarea
              name="requirement"
              required
              rows={3}
              defaultValue={item.action}
            />
          </label>
          <label>
            验收条件
            <textarea
              name="acceptance"
              required
              rows={2}
              defaultValue={item.success_metric}
            />
          </label>
          <p className="hint">创建后进入研发调研与审批流程。</p>
          <button
            className="primary"
            disabled={busy || !projects.some((p) => p.my_role !== "viewer")}
          >
            创建研发任务
          </button>
          <button type="button" onClick={() => setMode("")}>
            取消
          </button>
        </form>
      )}
      {(item.can_feedback ?? (canManage || item.assignee_id === user.id)) &&
        !["done", "dismissed"].includes(item.status) && (
          <form
            className="action-form"
            key={item.status + item.outcome}
            onSubmit={(e) => {
              e.preventDefault();
              const f = new FormData(e.currentTarget);
              const status = String(f.get("status"));
              const outcome = String(f.get("outcome") || "");
              if (["done", "dismissed"].includes(status) && !outcome.trim()) {
                setError("完成或不采纳时请填写结果与依据");
                return;
              }
              act(`/actions/${item.id}/feedback`, { status, outcome });
            }}
          >
            <div className="field-grid">
              <label>
                行动进展
                <select
                  name="status"
                  defaultValue={
                    item.status === "proposed" ? "accepted" : item.status
                  }
                >
                  {item.status === "proposed" && (
                    <option value="accepted">已采纳</option>
                  )}
                  <option value="in_progress">执行中</option>
                  <option value="done">已完成</option>
                  <option value="dismissed">不采纳</option>
                </select>
              </label>
              <label>
                结果与依据
                <textarea name="outcome" rows={2} defaultValue={item.outcome} />
              </label>
            </div>
            <button disabled={busy}>保存反馈</button>
          </form>
        )}
    </article>
  );
}
export function ActionInbox({ user }: { user: User }) {
  const [items, setItems] = useState<ActionItem[]>([]),
    [approvals, setApprovals] = useState<ApprovalItem[]>([]),
    [approving, setApproving] = useState(""),
    [acceptances, setAcceptances] = useState<AcceptanceItem[]>([]),
    [accepting, setAccepting] = useState(""),
    [acceptanceNotice, setAcceptanceNotice] = useState(""),
    [notifications, setNotifications] = useState<
      { id: string; message: string; read: boolean; created: number }[]
    >([]),
    [error, setError] = useState(""),
    [loading, setLoading] = useState(true),
    [filter, setFilter] = useState("active");
  const load = async () => {
    const [r, approvals, acceptance] = await Promise.all([
      api("/actions/inbox"),
      api("/orchestration/inbox"),
      api("/acceptance/inbox"),
    ]);
    setItems(r.items);
    setNotifications(r.notifications);
    setApprovals(approvals.items);
    setAcceptances(acceptance.items);
  };
  useEffect(() => {
    load()
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  }, []);
  const visible = items.filter(
    (i) =>
      filter === "all" ||
      (filter === "overdue"
        ? i.overdue
        : !["done", "dismissed"].includes(i.status)),
  );
  return (
    <>
      <div className="page-title">
        <div>
          <div className="eyebrow">MY ACTIONS</div>
          <h1>我的行动</h1>
          <p>查看分配给你的行动，跟进期限并提交执行结果。</p>
        </div>
        <button
          disabled={loading}
          onClick={() => {
            setLoading(true);
            setError("");
            load()
              .catch((e) => setError(e.message))
              .finally(() => setLoading(false));
          }}
        >
          刷新
        </button>
      </div>
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      {loading && <p className="hint">正在加载行动…</p>}
      {approvals.length > 0 && (
        <section className="panel">
          <h2>待我审批的资源</h2>
          <p className="hint">
            这里只展示你负责资源的方案。批准后，任务仍需其他资源负责人通过才能执行。
          </p>
          {approvals.map((item) => (
            <article
              className="recommendation"
              key={`${item.kind}/${item.job_id}/${item.resource_id}`}
            >
              <div className="section-heading">
                <h3>{item.title}</h3>
                <span className="badge">
                  {item.kind === "development" ? "研发" : "分析"} · 版本{" "}
                  {item.version}
                </span>
              </div>
              <p>负责资源：{item.name}</p>
              {item.scoped_plan != null && (
                <details>
                  <summary>查看本资源实施方案</summary>
                  <EvidenceValue value={item.scoped_plan} />
                </details>
              )}
              <button
                className="primary"
                disabled={Boolean(approving) || !item.can_approve}
                onClick={async () => {
                  setApproving(item.resource_id);
                  setError("");
                  try {
                    await api(
                      `/orchestration/${item.kind}/${item.job_id}/approve`,
                      {
                        version: item.version,
                        resource_ids: [item.resource_id],
                      },
                    );
                    await load();
                  } catch (e) {
                    setError((e as Error).message);
                  } finally {
                    setApproving("");
                  }
                }}
              >
                {approving === item.resource_id ? "正在批准…" : "批准此资源"}
              </button>
            </article>
          ))}
        </section>
      )}
      <section className="panel">
        <h2>待我验收的资源</h2>
        <p className="hint">
          核对自己负责资源的交付结果，全部资源负责人验收后任务才完成。
        </p>
        {acceptanceNotice && (
          <p className="hint" role="status">
            {acceptanceNotice}
          </p>
        )}
        {!loading && !acceptances.length && (
          <p className="hint">暂无待你验收的资源。</p>
        )}
        {acceptances.map((item) => (
          <article
            className="recommendation"
            key={`${item.kind}/${item.job_id}/${item.resource_id}`}
          >
            <div className="section-heading">
              <h3>{item.title}</h3>
              <span className="badge">
                {item.kind === "development" ? "研发" : "分析"} · 版本{" "}
                {item.version}
              </span>
            </div>
            <p>负责资源：{item.name}</p>
            {item.scoped_result != null && (
              <details>
                <summary>查看本资源交付证据</summary>
                <EvidenceValue value={item.scoped_result} />
              </details>
            )}
            <button
              className="primary"
              disabled={Boolean(accepting) || !item.can_accept}
              onClick={async () => {
                const key = `${item.kind}/${item.job_id}/${item.resource_id}`;
                setAccepting(key);
                setAcceptanceNotice("");
                setError("");
                try {
                  const result = await api(
                    `/acceptance/${item.kind}/${item.job_id}`,
                    {
                      version: item.version,
                      resource_ids: [item.resource_id],
                    },
                  );
                  await load();
                  setAcceptanceNotice(
                    result.completed
                      ? "全部资源已验收，任务已完成。"
                      : "此资源已验收，等待其他资源负责人验收。",
                  );
                } catch (e) {
                  setError((e as Error).message);
                } finally {
                  setAccepting("");
                }
              }}
            >
              {accepting === `${item.kind}/${item.job_id}/${item.resource_id}`
                ? "正在验收…"
                : "确认此资源验收"}
            </button>
          </article>
        ))}
      </section>
      {notifications.length > 0 && (
        <section className="panel">
          <h2>站内通知</h2>
          {notifications.map((n) => (
            <div className="approval-row" key={n.id}>
              <div>
                <p>{n.message}</p>
                <small>{new Date(n.created * 1000).toLocaleString()}</small>
              </div>
              {!n.read && (
                <button
                  onClick={async () => {
                    try {
                      await api(`/actions/notifications/${n.id}/read`, {});
                      await load();
                    } catch (e) {
                      setError((e as Error).message);
                    }
                  }}
                >
                  标记已读
                </button>
              )}
              {n.read && <span className="hint">已读</span>}
            </div>
          ))}
        </section>
      )}
      <section className="panel">
        <div className="section-heading">
          <h2>行动清单</h2>
          <label className="inline-label">
            显示
            <select value={filter} onChange={(e) => setFilter(e.target.value)}>
              <option value="active">未完成</option>
              <option value="overdue">已逾期</option>
              <option value="all">全部</option>
            </select>
          </label>
        </div>
        {!loading && !error && !visible.length && (
          <div className="empty">
            <h3>
              {items.length ? "没有符合条件的行动" : "暂无分配给你的行动"}
            </h3>
            <p>
              {items.length
                ? "切换筛选查看其他行动。"
                : "负责人分配建议后，会在这里显示行动与期限。"}
            </p>
          </div>
        )}
        {visible.map((item) => (
          <ActionCard key={item.id} item={item} user={user} onChanged={load} />
        ))}
      </section>
    </>
  );
}

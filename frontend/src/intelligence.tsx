import { useEffect, useMemo, useState } from "react";
import {
  BarChart3,
  Check,
  ChevronRight,
  Database,
  FileText,
  Lightbulb,
  Plus,
  Settings2,
  Users,
  X,
} from "lucide-react";
import { api, type User } from "./session";

type Metric = {
  id: string;
  name: string;
  description: string;
  table_name: string;
  aggregation: string;
  value_column?: string;
  date_column?: string;
  dimensions: string[];
};
export type DataSource = {
  id: string;
  name: string;
  kind: "sqlite" | "csv";
  path: string;
  description: string;
  my_role: "owner" | "analyst" | "viewer";
  metrics: Metric[];
};
type DirectoryUser = { id: string; username: string; display_name: string };
type Member = DirectoryUser & { user_id: string; role: string; active: number };
type Config = { configured: boolean };
type Job = {
  id: string;
  source_id: string;
  source_name?: string;
  title: string;
  question: string;
  report_kind: "report" | "strategy";
  status: string;
  plan?: string;
  query_spec?: {
    items: { metric_id: string; dimensions: string[] }[];
    grain: string;
    start_date?: string;
    end_date?: string;
  };
  version: number;
  can_edit?: boolean;
  can_review?: boolean;
  created_by_name?: string;
  approved_by_name?: string;
  events?: {
    id: number;
    kind: string;
    message: string;
    created: number;
    actor_name?: string;
  }[];
  result?: {
    report: string;
    source: string;
    row_count: number;
    rows: Record<string, unknown>[];
    generated_at: number;
  };
};

const roles: Record<string, string> = {
  owner: "数据负责人",
  analyst: "分析师",
  viewer: "只读成员",
};
const statusNames: Record<string, string> = {
  pending: "等待规划",
  planning: "规划分析",
  awaiting_approval: "待方案审批",
  queued: "等待分析",
  analyzing: "计算指标",
  reporting: "生成报告",
  review: "待报告验收",
  completed: "已完成",
  failed: "分析失败",
  cancelled: "已取消",
  interrupted: "已中断",
};
const aggregationNames: Record<string, string> = {
  sum: "求和",
  avg: "平均值",
  count: "记录数",
  count_distinct: "去重计数",
  min: "最小值",
  max: "最大值",
};

function Modal({
  title,
  close,
  children,
}: {
  title: string;
  close: () => void;
  children: React.ReactNode;
}) {
  return (
    <div className="overlay">
      <section
        className="modal intelligence-modal"
        role="dialog"
        aria-modal="true"
      >
        <div className="section-heading">
          <h2>{title}</h2>
          <button onClick={close} aria-label="关闭">
            <X size={18} />
          </button>
        </div>
        {children}
      </section>
    </div>
  );
}

export function DataCenter({ user }: { user: User }) {
  const [sources, setSources] = useState<DataSource[]>([]);
  const [selected, setSelected] = useState<DataSource | null>(null);
  const [mode, setMode] = useState("");
  const [schema, setSchema] = useState<Record<string, string[]>>({});
  const [members, setMembers] = useState<Member[]>([]);
  const [directory, setDirectory] = useState<DirectoryUser[]>([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const load = () => api("/data-sources").then(setSources);
  useEffect(() => {
    load().catch((e) => setError(e.message));
  }, []);
  const act = async (fn: () => Promise<void>) => {
    setBusy(true);
    setError("");
    try {
      await fn();
      await load();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  const openMetric = async (source: DataSource) => {
    setSelected(source);
    setError("");
    try {
      setSchema(await api("/data-sources/" + source.id + "/schema"));
      setMode("metric");
    } catch (e) {
      setError((e as Error).message);
    }
  };
  const openMembers = async (source: DataSource) => {
    setSelected(source);
    setError("");
    try {
      const [m, d] = await Promise.all([
        api("/data-sources/" + source.id + "/members"),
        api("/directory"),
      ]);
      setMembers(m);
      setDirectory(d);
      setMode("members");
    } catch (e) {
      setError((e as Error).message);
    }
  };
  return (
    <>
      <div className="eyebrow">ENTERPRISE DATA CONTEXT</div>
      <div className="page-title">
        <div>
          <h1>数据与指标</h1>
          <p>连接受控数据文件，统一业务指标口径，为分析任务提供可信上下文。</p>
        </div>
        {user.role === "admin" && (
          <button className="primary" onClick={() => setMode("source")}>
            <Plus size={17} />
            连接数据源
          </button>
        )}
      </div>
      {error && <p className="error">{error}</p>}
      <div className="project-grid source-grid">
        {sources.map((source) => (
          <article className="project source" key={source.id}>
            <div className="source-head">
              <Database size={25} />
              <span className="badge">{source.kind.toUpperCase()}</span>
            </div>
            <h2>
              {source.name}{" "}
              <span className="badge">{roles[source.my_role]}</span>
            </h2>
            <p>{source.description || "尚未填写数据说明"}</p>
            <code>{source.path}</code>
            <div className="metric-list">
              <div className="command-label">
                业务指标 · {source.metrics.length}
              </div>
              {source.metrics.map((metric) => (
                <div className="metric-row" key={metric.id}>
                  <strong>{metric.name}</strong>
                  <small>
                    {aggregationNames[metric.aggregation]} · {metric.table_name}
                    {metric.dimensions.length
                      ? " · 维度：" + metric.dimensions.join("、")
                      : ""}
                  </small>
                </div>
              ))}
              {!source.metrics.length && (
                <p className="hint">尚未配置业务指标。</p>
              )}
            </div>
            {source.my_role === "owner" && (
              <div className="compact-actions">
                <button onClick={() => openMetric(source)}>
                  <Settings2 size={15} />
                  配置指标
                </button>
                <button onClick={() => openMembers(source)}>
                  <Users size={15} />
                  成员权限
                </button>
              </div>
            )}
          </article>
        ))}
      </div>
      {!sources.length && (
        <div className="empty">
          <Database size={35} />
          <h3>还没有可访问的数据源</h3>
          <p>管理员连接 SQLite 或 CSV，配置业务指标并分配分析权限。</p>
        </div>
      )}
      {mode === "source" && (
        <Modal title="连接企业数据源" close={() => setMode("")}>
          <form
            onSubmit={(event) => {
              event.preventDefault();
              const form = new FormData(event.currentTarget);
              act(async () => {
                await api("/data-sources", {
                  name: form.get("name"),
                  kind: form.get("kind"),
                  path: form.get("path"),
                  description: form.get("description"),
                });
                setMode("");
              });
            }}
          >
            <label>
              名称
              <input name="name" required placeholder="例如：销售订单数据" />
            </label>
            <label>
              类型
              <select name="kind">
                <option value="sqlite">SQLite</option>
                <option value="csv">CSV</option>
              </select>
            </label>
            <label>
              服务器文件绝对路径
              <input
                name="path"
                required
                placeholder="需位于 ESI_DATA_ROOTS 允许目录"
              />
            </label>
            <label>
              数据说明
              <textarea
                name="description"
                rows={3}
                placeholder="来源、刷新周期、负责人和适用范围"
              />
            </label>
            <p className="hint">
              系统只执行只读聚合查询，不向模型发送原始明细。
            </p>
            {error && <p className="error">{error}</p>}
            <button className="primary submit" disabled={busy}>
              验证并连接
            </button>
          </form>
        </Modal>
      )}
      {mode === "metric" && selected && (
        <MetricModal
          source={selected}
          schema={schema}
          busy={busy}
          error={error}
          close={() => setMode("")}
          save={(data) =>
            act(async () => {
              await api("/data-sources/" + selected.id + "/metrics", data);
              setMode("");
            })
          }
        />
      )}
      {mode === "members" && selected && (
        <Modal title={selected.name + " · 数据权限"} close={() => setMode("")}>
          <p className="hint">
            负责人管理指标和审批；分析师创建任务；只读成员查看报告。
          </p>
          <div className="member-list">
            {members.map((member) => (
              <form
                className="member-row"
                key={member.user_id}
                onSubmit={(event) => {
                  event.preventDefault();
                  const role = String(
                    new FormData(event.currentTarget).get("role"),
                  );
                  act(async () => {
                    await api("/data-sources/" + selected.id + "/members", {
                      user_id: member.user_id,
                      role,
                    });
                    await openMembers(selected);
                  });
                }}
              >
                <div>
                  <strong>{member.display_name}</strong>
                  <small>{member.username}</small>
                </div>
                <select name="role" defaultValue={member.role}>
                  <option value="owner">数据负责人</option>
                  <option value="analyst">分析师</option>
                  <option value="viewer">只读成员</option>
                  <option value="remove">移出数据源</option>
                </select>
                <button disabled={busy}>保存</button>
              </form>
            ))}
          </div>
          <form
            className="add-member"
            onSubmit={(event) => {
              event.preventDefault();
              const form = new FormData(event.currentTarget);
              act(async () => {
                await api("/data-sources/" + selected.id + "/members", {
                  user_id: form.get("user_id"),
                  role: form.get("role"),
                });
                await openMembers(selected);
              });
            }}
          >
            <h3>添加成员</h3>
            <label>
              企业账号
              <select name="user_id" required defaultValue="">
                <option value="" disabled>
                  选择账号
                </option>
                {directory
                  .filter(
                    (item) =>
                      !members.some((member) => member.user_id === item.id),
                  )
                  .map((item) => (
                    <option key={item.id} value={item.id}>
                      {item.display_name} · {item.username}
                    </option>
                  ))}
              </select>
            </label>
            <label>
              权限
              <select name="role">
                <option value="analyst">分析师</option>
                <option value="viewer">只读成员</option>
                <option value="owner">数据负责人</option>
              </select>
            </label>
            {error && <p className="error">{error}</p>}
            <button className="primary submit" disabled={busy}>
              添加成员
            </button>
          </form>
        </Modal>
      )}
    </>
  );
}

function MetricModal({
  source,
  schema,
  busy,
  error,
  close,
  save,
}: {
  source: DataSource;
  schema: Record<string, string[]>;
  busy: boolean;
  error: string;
  close: () => void;
  save: (data: unknown) => void;
}) {
  const tables = Object.keys(schema);
  const [table, setTable] = useState(tables[0] || "");
  const columns = schema[table] || [];
  return (
    <Modal title={source.name + " · 新建指标"} close={close}>
      <form
        onSubmit={(event) => {
          event.preventDefault();
          const form = new FormData(event.currentTarget);
          save({
            name: form.get("name"),
            description: form.get("description"),
            table_name: form.get("table_name"),
            aggregation: form.get("aggregation"),
            value_column: form.get("value_column") || null,
            date_column: form.get("date_column") || null,
            dimensions: String(form.get("dimensions") || "")
              .split(",")
              .map((x) => x.trim())
              .filter(Boolean),
          });
        }}
      >
        <label>
          指标名称
          <input name="name" required placeholder="例如：销售额" />
        </label>
        <label>
          口径说明
          <textarea
            name="description"
            required
            rows={3}
            placeholder="例如：已支付订单金额求和，不含取消订单"
          />
        </label>
        <label>
          数据表
          <select
            name="table_name"
            value={table}
            onChange={(e) => setTable(e.target.value)}
          >
            {tables.map((item) => (
              <option key={item}>{item}</option>
            ))}
          </select>
        </label>
        <label>
          聚合方式
          <select name="aggregation">
            <option value="sum">求和</option>
            <option value="avg">平均值</option>
            <option value="count">记录数</option>
            <option value="count_distinct">去重计数</option>
            <option value="min">最小值</option>
            <option value="max">最大值</option>
          </select>
        </label>
        <label>
          数值字段
          <select name="value_column">
            <option value="">记录数无需选择</option>
            {columns.map((item) => (
              <option key={item}>{item}</option>
            ))}
          </select>
        </label>
        <label>
          日期字段
          <select name="date_column">
            <option value="">不支持时间筛选</option>
            {columns.map((item) => (
              <option key={item}>{item}</option>
            ))}
          </select>
        </label>
        <label>
          允许分析的维度
          <input
            name="dimensions"
            placeholder={columns.slice(0, 3).join(", ")}
          />
          <small>使用英文逗号分隔，必须是表中字段。</small>
        </label>
        <div className="schema-preview">可用字段：{columns.join("、")}</div>
        {error && <p className="error">{error}</p>}
        <button className="primary submit" disabled={busy || !table}>
          保存指标口径
        </button>
      </form>
    </Modal>
  );
}

export function AnalysisCenter({
  sources,
  config,
}: {
  sources: DataSource[];
  config: Config | null;
}) {
  const [jobs, setJobs] = useState<Job[]>([]),
    [selected, setSelected] = useState<Job | null>(null),
    [mode, setMode] = useState(""),
    [plan, setPlan] = useState(""),
    [error, setError] = useState(""),
    [busy, setBusy] = useState(false);
  const writable = sources.filter(
    (source) => source.my_role !== "viewer" && source.metrics.length,
  );
  const load = () => api("/analysis-jobs").then(setJobs);
  useEffect(() => {
    load().catch((e) => setError(e.message));
    const timer = setInterval(() => {
      load().catch(() => {});
      if (selected) open(selected.id, false);
    }, 2500);
    return () => clearInterval(timer);
  }, [selected?.id]);
  const open = async (id: string, switchView = true) => {
    const job = await api("/analysis-jobs/" + id);
    setSelected(job);
    setPlan(job.plan || "");
    if (switchView) setMode("detail");
  };
  const act = async (fn: () => Promise<void>) => {
    setBusy(true);
    setError("");
    try {
      await fn();
      await load();
      if (selected) await open(selected.id, false);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  if (mode === "detail" && selected)
    return (
      <AnalysisDetail
        job={selected}
        plan={plan}
        setPlan={setPlan}
        error={error}
        busy={busy}
        back={() => {
          setMode("");
          setSelected(null);
          load();
        }}
        action={(name, body) =>
          act(() => api("/analysis-jobs/" + selected.id + "/" + name, body))
        }
      />
    );
  return (
    <>
      <div className="eyebrow">DECISION INTELLIGENCE</div>
      <div className="page-title">
        <div>
          <h1>经营分析</h1>
          <p>围绕统一指标生成经营报告或战略建议，并由数据负责人审批和验收。</p>
        </div>
        <button
          className="primary"
          disabled={!writable.length || !config?.configured}
          onClick={() => setMode("new")}
        >
          <Plus size={17} />
          新建分析任务
        </button>
      </div>
      {error && <p className="error">{error}</p>}
      <div className="analysis-kinds">
        <div>
          <BarChart3 />
          <strong>经营报告</strong>
          <span>趋势、结构、异常和行动建议</span>
        </div>
        <div>
          <Lightbulb />
          <strong>战略分析</strong>
          <span>假设、备选方案、风险和验证指标</span>
        </div>
      </div>
      <div className="section-heading">
        <h2>分析任务</h2>
        <span>{jobs.length} 个任务</span>
      </div>
      <div className="task-list">
        {jobs.map((job) => (
          <button
            className="task-row"
            key={job.id}
            onClick={() => open(job.id)}
          >
            <div className="task-icon">
              {job.report_kind === "strategy" ? (
                <Lightbulb size={20} />
              ) : (
                <FileText size={20} />
              )}
            </div>
            <div className="task-name">
              <strong>{job.title}</strong>
              <small>
                {sources.find((source) => source.id === job.source_id)?.name ||
                  job.source_id}{" "}
                · {job.report_kind === "strategy" ? "战略分析" : "经营报告"}
              </small>
            </div>
            <span className={"badge " + job.status}>
              {statusNames[job.status]}
            </span>
            <ChevronRight size={17} />
          </button>
        ))}
      </div>
      {!jobs.length && (
        <div className="empty">
          <BarChart3 size={35} />
          <h3>从一个经营问题开始</h3>
          <p>选择配置好指标的数据源，描述要回答的问题。</p>
        </div>
      )}
      {mode === "new" && (
        <Modal title="新建经营分析任务" close={() => setMode("")}>
          <form
            onSubmit={(event) => {
              event.preventDefault();
              const form = new FormData(event.currentTarget);
              act(async () => {
                const job = await api("/analysis-jobs", {
                  source_id: form.get("source_id"),
                  title: form.get("title"),
                  question: form.get("question"),
                  report_kind: form.get("report_kind"),
                });
                setMode("");
                await open(job.id);
              });
            }}
          >
            <label>
              数据源
              <select name="source_id" required defaultValue="">
                <option value="" disabled>
                  选择数据源
                </option>
                {writable.map((source) => (
                  <option key={source.id} value={source.id}>
                    {source.name}
                  </option>
                ))}
              </select>
            </label>
            <label>
              任务类型
              <select name="report_kind">
                <option value="report">经营分析报告</option>
                <option value="strategy">战略分析建议</option>
              </select>
            </label>
            <label>
              标题
              <input
                name="title"
                required
                placeholder="例如：第三季度区域销售复盘"
              />
            </label>
            <label>
              需要回答的问题
              <textarea
                name="question"
                required
                rows={6}
                placeholder="说明分析目标、关注时间、业务问题和使用场景。系统只会使用已配置的指标与维度。"
              />
            </label>
            <p className="hint">
              AI 先生成分析方案，负责人批准后才计算指标和生成报告。
            </p>
            {error && <p className="error">{error}</p>}
            <button className="primary submit" disabled={busy}>
              创建并生成方案
            </button>
          </form>
        </Modal>
      )}
    </>
  );
}

function AnalysisDetail({
  job,
  plan,
  setPlan,
  error,
  busy,
  back,
  action,
}: {
  job: Job;
  plan: string;
  setPlan: (value: string) => void;
  error: string;
  busy: boolean;
  back: () => void;
  action: (name: string, body?: unknown) => void;
}) {
  const columns = useMemo(
    () =>
      job.result?.rows.length
        ? Array.from(
            new Set(job.result.rows.flatMap((row) => Object.keys(row))),
          )
        : [],
    [job.result],
  );
  return (
    <>
      <button className="back" onClick={back}>
        ← 返回分析任务
      </button>
      <div className="page-title">
        <div>
          <div className="eyebrow">ANALYSIS / {job.id}</div>
          <h1>{job.title}</h1>
          <p>
            {job.source_name} ·{" "}
            <span className={"badge " + job.status}>
              {statusNames[job.status]}
            </span>
          </p>
        </div>
        {job.can_edit &&
          ![
            "completed",
            "failed",
            "cancelled",
            "interrupted",
            "review",
          ].includes(job.status) && (
            <button onClick={() => action("cancel", {})}>取消任务</button>
          )}
      </div>
      {error && <p className="error">{error}</p>}
      <section className="panel">
        <h2>分析问题</h2>
        <p className="hint">
          创建人：{job.created_by_name} · 审批人：{job.approved_by_name}
        </p>
        <p className="preserve">{job.question}</p>
      </section>
      <section className="panel">
        <div className="section-heading">
          <h2>分析方案</h2>
          <span>版本 {job.version}</span>
        </div>
        {job.status === "awaiting_approval" && job.can_edit ? (
          <>
            <textarea
              className="plan-editor"
              value={plan}
              onChange={(event) => setPlan(event.target.value)}
            />
            {job.query_spec && (
              <div className="query-summary">
                <strong>将执行的指标计划</strong>
                <code>{JSON.stringify(job.query_spec, null, 2)}</code>
              </div>
            )}
            <div className="actions">
              <button
                disabled={busy || plan === job.plan}
                onClick={() => action("plan", { plan, version: job.version })}
              >
                保存修改
              </button>
              <button
                className="primary"
                disabled={busy || !job.can_review || plan !== job.plan}
                onClick={() => action("approve", { version: job.version })}
              >
                <Check size={16} />
                {job.can_review ? "批准并生成报告" : "等待数据负责人批准"}
              </button>
            </div>
          </>
        ) : job.plan ? (
          <pre className="prose">{job.plan}</pre>
        ) : (
          <p className="hint">正在根据指标目录生成分析方案…</p>
        )}
      </section>
      {job.result && (
        <>
          <section className="panel report">
            <div className="section-heading">
              <h2>
                {job.report_kind === "strategy" ? "战略建议" : "经营分析报告"}
              </h2>
              <span>
                {new Date(job.result.generated_at * 1000).toLocaleString()}
              </span>
            </div>
            <pre className="prose">{job.result.report}</pre>
            <div className="actions">
              <a
                className="button-link"
                href={`/api/analysis-jobs/${job.id}/report`}
              >
                下载 Markdown 报告
              </a>
              <a
                className="button-link"
                href={`/api/analysis-jobs/${job.id}/evidence`}
              >
                下载指标 CSV
              </a>
              {job.status === "review" && job.can_review && (
                <button
                  className="primary"
                  onClick={() => action("complete", {})}
                >
                  <Check size={16} />
                  确认报告验收完成
                </button>
              )}
            </div>
          </section>
          <section className="panel">
            <div className="section-heading">
              <h2>指标证据</h2>
              <span>{job.result.row_count} 个聚合结果</span>
            </div>
            <div className="table-wrap">
              <table className="team-table">
                <thead>
                  <tr>
                    {columns.map((column) => (
                      <th key={column}>{column}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {job.result.rows.map((row, index) => (
                    <tr key={index}>
                      {columns.map((column) => (
                        <td key={column}>{String(row[column] ?? "")}</td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>
        </>
      )}
      <section className="panel">
        <h2>过程记录</h2>
        <div className="logs">
          {job.events?.map((event) => (
            <div className={"log " + event.kind} key={event.id}>
              <time>{new Date(event.created * 1000).toLocaleTimeString()}</time>
              <pre>
                {event.actor_name ? "[" + event.actor_name + "] " : "[系统] "}
                {event.message}
              </pre>
            </div>
          ))}
        </div>
      </section>
    </>
  );
}

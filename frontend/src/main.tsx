import React, { useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import {
  Activity,
  ArrowRight,
  Check,
  ChevronRight,
  Code2,
  Database,
  BarChart3,
  FileCode2,
  FolderGit2,
  GitBranch,
  Layers,
  Loader2,
  Plus,
  Settings,
  ShieldCheck,
  Terminal,
  X,
  Users,
  LogOut,
} from "lucide-react";
import "./style.css";
import { api, SessionGate, PasswordForm, type User } from "./session";
import { TeamAdmin, ProjectMembers, AuditLog, roleNames } from "./team";
import { AnalysisCenter, DataCenter, type DataSource } from "./intelligence";

type Project = {
  id: string;
  name: string;
  path: string;
  branch: string;
  checks: string[];
  my_role: string;
};
type Task = {
  id: string;
  project_id: string;
  title: string;
  requirement: string;
  acceptance: string;
  status: string;
  plan: string;
  version: number;
  worktree: string;
  base: string;
  can_edit: boolean;
  can_review: boolean;
  created_by_name: string;
  approved_by_name: string;
  completed_by_name: string;
  events?: {
    id: number;
    kind: string;
    message: string;
    created: number;
    actor_name?: string;
  }[];
  result?: {
    summary: string;
    diff: string;
    checks: { command: string; exit_code: number; output: string }[];
  };
};
type Config = {
  execution: {
    mode: string;
    available: boolean;
    message: string;
    image: string;
  };
  configured: boolean;
  base_url: string;
  model: string;
  mode: string;
};
const names: Record<string, string> = {
  pending: "等待分析",
  analyzing: "分析仓库",
  awaiting_approval: "待方案确认",
  queued: "等待执行",
  running: "编写代码",
  verifying: "验证中",
  review: "待结果审查",
  completed: "已完成",
  failed: "执行失败",
  cancelled: "已取消",
  interrupted: "已中断",
};
const terminal = ["completed", "failed", "cancelled", "interrupted", "review"];
function App({
  user,
  onLogout,
  onUserChanged,
}: {
  user: User;
  onLogout: () => Promise<void>;
  onUserChanged: (u: User) => void;
}) {
  const [memberProject, setMemberProject] = useState<Project | null>(null);
  const [passwordOpen, setPasswordOpen] = useState(false);
  const isAdmin = user.role === "admin";
  const [projects, setProjects] = useState<Project[]>([]),
    [dataSources, setDataSources] = useState<DataSource[]>([]),
    [tasks, setTasks] = useState<Task[]>([]),
    [selected, setSelected] = useState<Task | null>(null),
    [config, setConfig] = useState<Config | null>(null);
  const [view, setView] = useState("tasks"),
    [modal, setModal] = useState(""),
    [tab, setTab] = useState("overview"),
    [error, setError] = useState(""),
    [busy, setBusy] = useState(false),
    [plan, setPlan] = useState("");
  const refresh = async () => {
    const [p, d, t, c] = await Promise.all([
      api("/projects"),
      api("/data-sources"),
      api("/tasks"),
      api("/settings"),
    ]);
    setProjects(p);
    setDataSources(d);
    setTasks(t);
    setConfig(c);
  };
  useEffect(() => {
    refresh().catch((e) => setError(e.message));
  }, []);
  useEffect(() => {
    setPlan(selected?.plan || "");
  }, [selected?.id, selected?.version]);
  useEffect(() => {
    if (view !== "tasks") return;
    const timer = setInterval(
      () =>
        api("/tasks")
          .then(setTasks)
          .catch(() => {}),
      3000,
    );
    return () => clearInterval(timer);
  }, [view]);
  const openTask = async (id: string) => {
    const t = await api("/tasks/" + id);
    setSelected(t);
    setPlan(t.plan || "");
    setView("detail");
    setTab("overview");
  };
  useEffect(() => {
    if (!selected) return;
    const id = selected.id;
    const stream = new EventSource("/api/tasks/" + id + "/stream");
    const load = () =>
      api("/tasks/" + id)
        .then((t) => setSelected(t))
        .catch(() => {});
    stream.addEventListener("revoked", () => {
      stream.close();
      setSelected(null);
      setView("tasks");
      setError("访问权限已变化，请重新选择项目");
      refresh().catch(() => {});
    });
    stream.onmessage = load;
    stream.addEventListener("status", load);
    return () => stream.close();
  }, [selected?.id]);
  const action = async (fn: () => Promise<void>) => {
    setBusy(true);
    setError("");
    try {
      await fn();
      await refresh();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  const mutate = async (suffix: string, body: unknown = {}) => {
    if (!selected) return;
    await api("/tasks/" + selected.id + "/" + suffix, body);
    const t = await api("/tasks/" + selected.id);
    setSelected(t);
    setPlan(t.plan || "");
  };
  const writableProjects = projects.filter((p) => p.my_role !== "viewer");
  const running = tasks.filter((t) =>
    ["pending", "queued", "analyzing", "running", "verifying"].includes(
      t.status,
    ),
  ).length;
  return (
    <div className="app">
      <aside>
        <a
          className="brand"
          href="#"
          onClick={(e) => {
            e.preventDefault();
            setView("tasks");
            refresh();
          }}
        >
          <div className="brand-mark">
            <Layers size={23} />
          </div>
          <span>
            ESI<span className="brand-sub">Enterprise Super Intelligence</span>
          </span>
        </a>
        <div className="workspace-label">
          工作空间 <span>TEAM</span>
        </div>
        <nav>
          <button
            className={view === "analysis" ? "active" : ""}
            onClick={() => {
              setView("analysis");
              refresh();
            }}
          >
            <BarChart3 size={18} />
            经营分析
          </button>
          <button
            className={view === "data" ? "active" : ""}
            onClick={() => setView("data")}
          >
            <Database size={18} />
            数据与指标
          </button>
          <button
            className={["tasks", "detail"].includes(view) ? "active" : ""}
            onClick={() => {
              setView("tasks");
              refresh();
            }}
          >
            <Code2 size={18} />
            研发任务 <span className="nav-count">{tasks.length}</span>
          </button>
          <button
            className={view === "projects" ? "active" : ""}
            onClick={() => setView("projects")}
          >
            <FolderGit2 size={18} />
            项目仓库
          </button>
          <button
            className={view === "settings" ? "active" : ""}
            onClick={() => setView("settings")}
          >
            <Settings size={18} />
            模型与设置
          </button>
          {isAdmin && (
            <>
              <button
                className={view === "team" ? "active" : ""}
                onClick={() => setView("team")}
              >
                <Users size={18} />
                团队与账号
              </button>
              <button
                className={view === "audit" ? "active" : ""}
                onClick={() => setView("audit")}
              >
                <ShieldCheck size={18} />
                操作审计
              </button>
            </>
          )}
        </nav>
        <div className="side-note">
          <ShieldCheck size={20} />
          <strong>人机协同，受控执行</strong>
          <p>每一份方案经过确认，每一次变更都有迹可循。</p>
        </div>
        <div className="user">
          <div className="avatar">{user.display_name.slice(0, 1)}</div>
          <div>
            {user.display_name}
            <small>{roleNames[user.role]} · v0.3</small>
          </div>
          <button className="logout" aria-label="退出登录" onClick={onLogout}>
            <LogOut size={16} />
          </button>
        </div>
        <button className="password-link" onClick={() => setPasswordOpen(true)}>
          修改密码
        </button>
      </aside>
      <div className="main">
        <header>
          <span>工作空间</span>
          <ChevronRight size={14} />
          <b>
            {view === "analysis"
              ? "经营分析"
              : view === "data"
                ? "数据与指标"
                : view === "team"
                  ? "团队与账号"
                  : view === "audit"
                    ? "操作审计"
                    : view === "projects"
                      ? "项目仓库"
                      : view === "settings"
                        ? "模型与设置"
                        : "研发任务"}
          </b>
          <div className="header-right">
            <span className={"dot " + (!config?.configured ? "off" : "")} />
            {config?.configured ? "模型已配置" : "模型待配置"}
          </div>
        </header>
        <main>
          {error && (
            <div className="error" role="alert">
              {error}
              <button aria-label="关闭错误" onClick={() => setError("")}>
                <X size={16} />
              </button>
            </div>
          )}
          {view === "analysis" && (
            <AnalysisCenter sources={dataSources} config={config} />
          )}
          {view === "data" && <DataCenter user={user} />}
          {view === "team" && isAdmin && <TeamAdmin current={user} />}
          {view === "audit" && isAdmin && <AuditLog />}
          {view === "tasks" && (
            <>
              <div className="eyebrow">BUILD WITH INTELLIGENCE</div>
              <div className="page-title">
                <div>
                  <h1>让需求，走向交付。</h1>
                  <p>从方案到代码，在一个可追踪的工作流中完成研发。</p>
                </div>
                <button
                  className="primary"
                  disabled={!writableProjects.length}
                  onClick={() => setModal("task")}
                >
                  <Plus size={17} />
                  新建研发任务
                </button>
              </div>
              <div className="stats">
                <div>
                  <span>全部任务</span>
                  <strong>{tasks.length.toString().padStart(2, "0")}</strong>
                  <Code2 />
                </div>
                <div>
                  <span>正在执行</span>
                  <strong>{running.toString().padStart(2, "0")}</strong>
                  <Activity />
                </div>
                <div>
                  <span>等待人工确认</span>
                  <strong>
                    {tasks
                      .filter((t) =>
                        ["awaiting_approval", "review"].includes(t.status),
                      )
                      .length.toString()
                      .padStart(2, "0")}
                  </strong>
                  <ShieldCheck />
                </div>
              </div>
              <div className="section-heading">
                <h2>研发任务</h2>
                <span>从明确目标开始</span>
              </div>
              <div className="task-list">
                {tasks.length ? (
                  tasks.map((t) => (
                    <button
                      className="task-row"
                      key={t.id}
                      onClick={() => action(() => openTask(t.id))}
                    >
                      <div className="task-icon">
                        <FileCode2 size={20} />
                      </div>
                      <div className="task-name">
                        <strong>{t.title}</strong>
                        <small>
                          {projects.find((p) => p.id === t.project_id)?.name}{" "}
                          <span>·</span> {t.id}
                        </small>
                      </div>
                      <span className={"badge " + t.status}>
                        {names[t.status]}
                      </span>
                      <ChevronRight size={17} />
                    </button>
                  ))
                ) : (
                  <div className="empty">
                    <div className="empty-icon">
                      <GitBranch size={32} />
                    </div>
                    <h3>下一个功能，从这里开始</h3>
                    <p>
                      连接一个代码仓库，描述需求，
                      <br />让 AI 准备方案，并在你确认后开始开发。
                    </p>
                    <button
                      onClick={() =>
                        writableProjects.length
                          ? setModal("task")
                          : isAdmin
                            ? setModal("project")
                            : setView("projects")
                      }
                    >
                      {writableProjects.length
                        ? "创建第一个任务"
                        : isAdmin
                          ? "连接第一个仓库"
                          : "查看授权项目"}
                      <ArrowRight size={16} />
                    </button>
                  </div>
                )}
              </div>
              <div className="workflow">
                <span>你的交付路径</span>
                {["提出需求", "方案确认", "代码实现", "验证与审查"].map(
                  (x, i) => (
                    <React.Fragment key={x}>
                      <div>
                        <i>{i + 1}</i>
                        {x}
                      </div>
                      {i < 3 && <ArrowRight size={14} />}
                    </React.Fragment>
                  ),
                )}
              </div>
            </>
          )}
          {view === "projects" && (
            <>
              <div className="page-title">
                <div>
                  <div className="eyebrow">CONNECTED REPOSITORIES</div>
                  <h1>项目仓库</h1>
                  <p>为研发任务提供代码上下文与验证方式。</p>
                </div>
                {isAdmin && (
                  <button
                    className="primary"
                    onClick={() => setModal("project")}
                  >
                    <Plus size={17} />
                    连接仓库
                  </button>
                )}
              </div>
              <div className="project-grid">
                {projects.map((p) => (
                  <article className="project" key={p.id}>
                    <FolderGit2 size={27} />
                    <h2>
                      {p.name}{" "}
                      <span className="badge">{roleNames[p.my_role]}</span>
                    </h2>
                    <code>{p.path}</code>
                    <p>
                      <GitBranch size={15} />
                      {p.branch}
                    </p>
                    <div className="command-label">验证命令</div>
                    {p.checks.map((c) => (
                      <pre key={c}>{c}</pre>
                    ))}
                    {p.my_role === "maintainer" && (
                      <button onClick={() => setMemberProject(p)}>
                        <Users size={16} />
                        管理项目成员
                      </button>
                    )}
                  </article>
                ))}
              </div>
              {!projects.length && (
                <div className="empty">
                  <FolderGit2 size={35} />
                  <h3>尚未连接仓库</h3>
                  <p>
                    请由系统管理员接入服务器上的代码仓库，并为团队分配项目角色。
                  </p>
                </div>
              )}
            </>
          )}
          {view === "settings" && (
            <>
              <div className="eyebrow">CONFIGURATION</div>
              <h1>模型与设置</h1>
              <p className="subtitle">模型凭证仅保留在服务端环境变量中。</p>
              <section className="panel">
                <h2>模型服务</h2>
                <dl>
                  <dt>配置状态</dt>
                  <dd>
                    {config?.configured
                      ? "已配置（实际连通性在任务调用时验证）"
                      : "尚未配置"}
                  </dd>
                  <dt>接口地址</dt>
                  <dd>{config?.base_url}</dd>
                  <dt>模型名称</dt>
                  <dd>{config?.model || "未设置"}</dd>
                  <dt>运行方式</dt>
                  <dd>{config?.mode}</dd>
                  <dt>代码执行</dt>
                  <dd>{config?.execution?.message}</dd>
                  <dt>执行镜像</dt>
                  <dd>{config?.execution?.image}</dd>
                </dl>
                <p>
                  在项目根目录创建 <code>.env</code>，填写以下配置后重启服务：
                </p>
                <pre>
                  MODEL_BASE_URL=https://api.openai.com/v1{"\n"}
                  MODEL_NAME=你的模型名称{"\n"}MODEL_API_KEY=你的服务端密钥
                </pre>
                <p>
                  团队环境默认使用 Docker
                  执行验证命令，仅挂载当前任务工作区，并禁用网络。请由管理员准备包含项目依赖的执行镜像。
                </p>
              </section>
            </>
          )}
          {view === "detail" && selected && (
            <>
              <button
                className="back"
                onClick={() => {
                  setView("tasks");
                  refresh();
                }}
              >
                ← 返回任务列表
              </button>
              <div className="page-title">
                <div>
                  <div className="eyebrow">TASK / {selected.id}</div>
                  <h1>{selected.title}</h1>
                  <p>
                    {projects.find((p) => p.id === selected.project_id)?.name} ·{" "}
                    <span className={"badge " + selected.status}>
                      {names[selected.status]}
                    </span>
                  </p>
                </div>
                {selected.can_edit && !terminal.includes(selected.status) && (
                  <button
                    disabled={busy}
                    onClick={() => action(() => mutate("cancel"))}
                  >
                    取消任务
                  </button>
                )}
              </div>
              {config?.execution && !config.execution.available && (
                <p className="error">
                  {config.execution.message}
                  。仍可进行方案分析，执行前请联系管理员。
                </p>
              )}
              <div className="tabs">
                {[
                  ["overview", "需求与方案"],
                  ["logs", "执行过程"],
                  ["result", "代码与验证"],
                ].map(([id, name]) => (
                  <button
                    key={id}
                    className={tab === id ? "selected" : ""}
                    onClick={() => setTab(id)}
                  >
                    {name}
                    {id === "logs" && selected.events?.length ? (
                      <span>{selected.events.length}</span>
                    ) : null}
                  </button>
                ))}
              </div>
              {tab === "overview" && (
                <>
                  <section className="panel">
                    <h2>需求说明</h2>
                    <p className="hint">
                      创建人：{selected.created_by_name} · 审批人：
                      {selected.approved_by_name}
                    </p>
                    <p className="preserve">{selected.requirement}</p>
                    <h3>验收条件</h3>
                    <p className="preserve">{selected.acceptance}</p>
                  </section>
                  <section className="panel">
                    <div className="section-heading">
                      <h2>实现方案</h2>
                      <span>版本 {selected.version}</span>
                    </div>
                    {selected.status === "awaiting_approval" &&
                    selected.can_edit ? (
                      <>
                        <textarea
                          className="plan-editor"
                          value={plan}
                          onChange={(e) => setPlan(e.target.value)}
                          aria-label="实现方案"
                        />
                        <div className="actions">
                          <button
                            disabled={busy || plan === selected.plan}
                            onClick={() =>
                              action(() =>
                                mutate("plan", {
                                  plan,
                                  version: selected.version,
                                }),
                              )
                            }
                          >
                            保存修改
                          </button>
                          <button
                            className="primary"
                            disabled={
                              busy ||
                              !selected.can_review ||
                              !config?.execution?.available ||
                              plan !== selected.plan
                            }
                            onClick={() =>
                              action(() =>
                                mutate("approve", {
                                  version: selected.version,
                                }),
                              )
                            }
                          >
                            <Check size={17} />
                            {selected.can_review
                              ? "批准方案并执行"
                              : "等待项目负责人批准"}
                          </button>
                        </div>
                        {plan !== selected.plan && (
                          <p className="hint">请先保存修改，再批准新版本。</p>
                        )}
                      </>
                    ) : selected.plan ? (
                      <pre className="prose">{selected.plan}</pre>
                    ) : (
                      <p className="hint">
                        {selected.status === "failed"
                          ? "方案生成失败，请查看执行过程。"
                          : "等待模型分析仓库并生成方案…"}
                      </p>
                    )}
                  </section>
                </>
              )}
              {tab === "logs" && (
                <section className="panel">
                  <h2>
                    <Terminal size={18} />
                    执行记录
                  </h2>
                  <div className="logs">
                    {selected.events?.map((e) => (
                      <div className={"log " + e.kind} key={e.id}>
                        <time>
                          {new Date(e.created * 1000).toLocaleTimeString()}
                        </time>
                        <pre>
                          {e.actor_name ? `[${e.actor_name}] ` : "[系统] "}
                          {e.message}
                        </pre>
                      </div>
                    ))}
                  </div>
                  {["running", "analyzing", "verifying"].includes(
                    selected.status,
                  ) && (
                    <div className="working">
                      <Loader2 size={16} />
                      任务正在后台执行
                    </div>
                  )}
                </section>
              )}
              {tab === "result" && (
                <>
                  {selected.result ? (
                    <>
                      <section className="panel">
                        <h2>交付总结</h2>
                        <p className="preserve">{selected.result.summary}</p>
                        <p className="hint">工作区：{selected.worktree}</p>
                        <p className="hint">
                          基准提交：{selected.result ? selected.base : ""}
                        </p>
                        {selected.status === "review" &&
                          selected.can_review && (
                            <button
                              className="primary"
                              disabled={busy}
                              onClick={() => action(() => mutate("complete"))}
                            >
                              <ShieldCheck size={17} />
                              确认审查完成
                            </button>
                          )}
                      </section>
                      <section className="panel">
                        <h2>验证结果</h2>
                        {selected.result.checks.map((c, i) => (
                          <details key={i}>
                            <summary>
                              <span
                                className={
                                  "badge " +
                                  (c.exit_code === 0 ? "completed" : "failed")
                                }
                              >
                                {c.exit_code === 0 ? "通过" : "失败"}
                              </span>{" "}
                              {c.command}
                            </summary>
                            <pre>{c.output || "命令无输出"}</pre>
                          </details>
                        ))}
                      </section>
                      <section className="panel diff-panel">
                        <h2>代码差异</h2>
                        <div className="diff">
                          {selected.result.diff.split("\n").map((line, i) => (
                            <div
                              key={i}
                              className={
                                line.startsWith("+")
                                  ? "addition"
                                  : line.startsWith("-")
                                    ? "deletion"
                                    : line.startsWith("@@")
                                      ? "hunk"
                                      : ""
                              }
                            >
                              <span>{i + 1}</span>
                              <code>{line || " "}</code>
                            </div>
                          ))}
                        </div>
                      </section>
                    </>
                  ) : (
                    <div className="empty">
                      <Code2 size={35} />
                      <h3>暂未产生交付结果</h3>
                      <p>
                        执行结束后，这里会显示代码差异和验证报告。
                        <br />
                        失败或中断时，可在执行记录中定位问题，工作区将保留。
                      </p>
                      {selected.worktree && (
                        <p className="hint">工作区：{selected.worktree}</p>
                      )}
                    </div>
                  )}
                </>
              )}
            </>
          )}
        </main>
        <footer>
          ESI / 企业超级智能平台 <span>目标清晰 · 过程可见 · 结果可验</span>
        </footer>
      </div>
      {memberProject && (
        <ProjectMembers
          project={memberProject}
          onClose={() => {
            setMemberProject(null);
            refresh();
          }}
        />
      )}
      {passwordOpen && (
        <div className="overlay">
          <PasswordForm
            onChanged={(u) => {
              onUserChanged(u);
              setPasswordOpen(false);
            }}
            onCancel={() => setPasswordOpen(false)}
          />
        </div>
      )}
      {modal && (
        <div className="overlay" onClick={() => !busy && setModal("")}>
          <section
            className="modal"
            role="dialog"
            aria-modal="true"
            aria-label={modal === "project" ? "连接仓库" : "新建任务"}
            onClick={(e) => e.stopPropagation()}
          >
            <div className="section-heading">
              <h2>{modal === "project" ? "连接本地仓库" : "新建研发任务"}</h2>
              <button
                aria-label="关闭"
                disabled={busy}
                onClick={() => setModal("")}
              >
                <X size={19} />
              </button>
            </div>
            <form
              onSubmit={(e) => {
                e.preventDefault();
                const f = new FormData(e.currentTarget);
                action(async () => {
                  if (modal === "project") {
                    await api("/projects", {
                      name: f.get("name"),
                      path: f.get("path"),
                      branch: f.get("branch"),
                      checks: String(f.get("checks"))
                        .split("\n")
                        .filter((x) => x.trim()),
                    });
                    setModal("");
                    setView("projects");
                  } else {
                    const t = await api("/tasks", {
                      project_id: f.get("project"),
                      title: f.get("title"),
                      requirement: f.get("requirement"),
                      acceptance: f.get("acceptance"),
                    });
                    setModal("");
                    await openTask(t.id);
                  }
                });
              }}
            >
              {modal === "project" ? (
                <>
                  <label>
                    项目名称
                    <input
                      name="name"
                      required
                      placeholder="例如：订单管理系统"
                    />
                  </label>
                  <label>
                    本地 Git 仓库绝对路径
                    <input
                      name="path"
                      required
                      placeholder="/Users/you/projects/order-service"
                    />
                  </label>
                  <label>
                    基准分支或提交
                    <input name="branch" required defaultValue="HEAD" />
                  </label>
                  <label>
                    验证命令（每行一条）
                    <textarea
                      name="checks"
                      required
                      placeholder="例如：python3 -m unittest discover -s tests"
                    />
                  </label>
                  <p className="hint">
                    命令会在隔离工作区中以本机用户运行。请只接入可信仓库并配置可信命令。
                  </p>
                </>
              ) : (
                <>
                  <label>
                    目标项目
                    <select name="project" required defaultValue="">
                      <option value="" disabled>
                        选择代码仓库
                      </option>
                      {writableProjects.map((p) => (
                        <option value={p.id} key={p.id}>
                          {p.name}
                        </option>
                      ))}
                    </select>
                  </label>
                  {!projects.length && (
                    <p className="hint">请先到“项目仓库”连接一个仓库。</p>
                  )}
                  <label>
                    任务标题
                    <input
                      name="title"
                      required
                      placeholder="例如：为订单列表增加手机号搜索"
                    />
                  </label>
                  <label>
                    需求说明
                    <textarea
                      name="requirement"
                      required
                      rows={4}
                      placeholder="描述要解决的问题、预期行为和约束…"
                    />
                  </label>
                  <label>
                    验收条件
                    <textarea
                      name="acceptance"
                      required
                      rows={3}
                      placeholder="说明如何判断功能正确，以及应保持的现有行为…"
                    />
                  </label>
                  <p className="hint">
                    创建后只分析仓库。批准实现方案后才会开始修改代码。
                  </p>
                </>
              )}
              {error && <p className="error">{error}</p>}
              <button
                className="primary submit"
                disabled={
                  busy ||
                  (modal === "task" &&
                    (!writableProjects.length || !config?.configured))
                }
              >
                {busy ? <Loader2 size={16} /> : <ArrowRight size={16} />}{" "}
                {busy
                  ? "处理中…"
                  : modal === "project"
                    ? "连接仓库"
                    : "创建任务并生成方案"}
              </button>
              {modal === "task" && !config?.configured && (
                <p className="hint">请先配置服务端模型。</p>
              )}
            </form>
          </section>
        </div>
      )}
    </div>
  );
}
createRoot(document.getElementById("root")!).render(
  <SessionGate>
    {(user, logout, updateUser) => (
      <App
        key={user.id}
        user={user}
        onLogout={logout}
        onUserChanged={updateUser}
      />
    )}
  </SessionGate>,
);

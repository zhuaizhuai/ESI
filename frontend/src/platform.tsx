import { useEffect, useState } from "react";
import { ArrowRight, BookOpen, Network, Plus, Search } from "lucide-react";
import { api, type User } from "./session";
import type { DataSource } from "./intelligence";

type Project = { id: string; name: string; my_role: string };
type System = {
  id: string;
  name: string;
  description: string;
  resources: { kind: "project" | "data_source"; resource_id: string }[];
  dependencies: { target_id: string; description: string }[];
};
type Knowledge = {
  id: string;
  system_id: string;
  title: string;
  content: string;
  source_url: string;
};
type Term = {
  id: string;
  name: string;
  definition: string;
  unit: string;
  metrics: { id: string; name: string; source_id: string }[];
};

export function PlatformCenter({
  user,
  projects,
  sources,
}: {
  user: User;
  projects: Project[];
  sources: DataSource[];
}) {
  const [systems, setSystems] = useState<System[]>([]);
  const [knowledge, setKnowledge] = useState<Knowledge[]>([]);
  const [terms, setTerms] = useState<Term[]>([]);
  const [search, setSearch] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const load = async () => {
    const [s, k, t] = await Promise.all([
      api("/platform/systems"),
      api("/platform/knowledge"),
      api("/platform/metric-terms"),
    ]);
    setSystems(s);
    setKnowledge(k);
    setTerms(t);
  };
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
  const resourceName = (kind: string, id: string) =>
    kind === "project"
      ? projects.find((p) => p.id === id)?.name || id
      : sources.find((s) => s.id === id)?.name || id;
  const canEdit = (system: System) =>
    user.role === "admin" ||
    system.resources.some((r) =>
      r.kind === "project"
        ? projects.find((p) => p.id === r.resource_id)?.my_role === "maintainer"
        : sources.find((s) => s.id === r.resource_id)?.my_role === "owner",
    );
  const visibleKnowledge = knowledge.filter((entry) => {
    const needle = search.trim().toLocaleLowerCase();
    return !needle || (entry.title + " " + entry.content).toLocaleLowerCase().includes(needle);
  });
  return (
    <>
      <div className="eyebrow">ENTERPRISE CONTEXT</div>
      <div className="page-title">
        <div>
          <h1>企业系统与知识</h1>
          <p>登记系统关系、连接已授权资源，并维护可追溯的业务知识与指标口径。</p>
        </div>
      </div>
      {error && <p className="error" role="alert">{error}</p>}

      {user.role === "admin" && (
        <section className="panel">
          <h2><Plus size={18} /> 登记企业系统</h2>
          <form className="platform-form" onSubmit={(event) => {
            event.preventDefault();
            const form = event.currentTarget;
            const values = new FormData(form);
            act(async () => {
              await api("/platform/systems", {
                name: values.get("name"), description: values.get("description"),
              });
              form.reset();
            });
          }}>
            <label>系统名称<input name="name" required maxLength={100} placeholder="例如：订单系统" /></label>
            <label>业务说明<input name="description" maxLength={2000} placeholder="职责、上下游与负责人" /></label>
            <button className="primary" disabled={busy}>登记</button>
          </form>
        </section>
      )}

      <div className="section-heading"><h2><Network size={19} /> 系统目录</h2><span>{systems.length} 个可访问系统</span></div>
      <div className="project-grid">
        {systems.map((system) => (
          <article className="project" key={system.id}>
            <h2>{system.name}</h2>
            <p>{system.description || "尚未填写业务说明"}</p>
            <div className="command-label">受控连接</div>
            {system.resources.map((resource) => (
              <p key={resource.kind + resource.resource_id}>
                {resource.kind === "project" ? "Git 仓库" : "数据源"}：
                {resourceName(resource.kind, resource.resource_id)}
              </p>
            ))}
            {!system.resources.length && <p className="hint">尚未关联资源，仅管理员可见。</p>}
            <div className="command-label">依赖系统</div>
            {system.dependencies.map((dep) => (
              <p key={dep.target_id}><ArrowRight size={14} /> {systems.find((s) => s.id === dep.target_id)?.name || dep.target_id} {dep.description}</p>
            ))}
            {!system.dependencies.length && <p className="hint">尚未登记依赖。</p>}
            {user.role === "admin" && (
              <>
                <form className="platform-form" onSubmit={(event) => {
                  event.preventDefault();
                  const form = event.currentTarget;
                  const values = new FormData(form);
                  act(async () => {
                    const resourceId = String(values.get("resource_id"));
                    await api(`/platform/systems/${system.id}/resources`, {
                      kind: projects.some((p) => p.id === resourceId) ? "project" : "data_source",
                      resource_id: resourceId,
                    });
                    form.reset();
                  });
                }}>
                  <label>关联资源<select name="resource_id" required defaultValue="">
                    <option value="" disabled>选择资源</option>
                    {projects.map((p) => <option key={p.id} value={p.id}>{p.name}（仓库）</option>)}
                    {sources.map((s) => <option key={s.id} value={s.id}>{s.name}（数据源）</option>)}
                  </select></label>
                  <button disabled={busy}>连接</button>
                </form>
                <form className="platform-form" onSubmit={(event) => {
                  event.preventDefault();
                  const form = event.currentTarget;
                  const values = new FormData(form);
                  act(async () => {
                    await api(`/platform/systems/${system.id}/dependencies`, {
                      target_id: values.get("target_id"), description: values.get("description"),
                    });
                    form.reset();
                  });
                }}>
                  <label>依赖系统<select name="target_id" required defaultValue="">
                    <option value="" disabled>选择系统</option>
                    {systems.filter((s) => s.id !== system.id).map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
                  </select></label>
                  <label>依赖说明<input name="description" maxLength={500} /></label>
                  <button disabled={busy}>添加依赖</button>
                </form>
              </>
            )}
          </article>
        ))}
      </div>
      {!systems.length && <p className="hint">尚无可访问的企业系统。请管理员登记并关联仓库或数据源。</p>}

      <div className="section-heading"><h2><BookOpen size={19} /> 业务知识</h2><span>{knowledge.length} 条有来源的知识</span></div>
      <label className="platform-search"><Search size={17} /> 搜索知识<input value={search} onChange={(e) => setSearch(e.target.value)} placeholder="名称或内容" /></label>
      {visibleKnowledge.map((entry) => (
        <section className="panel" key={entry.id}>
          <h3>{entry.title}</h3>
          <p className="hint">{systems.find((s) => s.id === entry.system_id)?.name} {entry.source_url && `· 来源：${entry.source_url}`}</p>
          <p className="preserve">{entry.content}</p>
        </section>
      ))}
      {systems.some(canEdit) && (
        <section className="panel">
          <h3>添加知识</h3>
          <form className="platform-form" onSubmit={(event) => {
            event.preventDefault();
            const form = event.currentTarget;
            const values = new FormData(form);
            act(async () => {
              await api("/platform/knowledge", {
                system_id: values.get("system_id"), title: values.get("title"),
                content: values.get("content"), source_url: values.get("source_url"),
              });
              form.reset();
            });
          }}>
            <label>所属系统<select name="system_id" required defaultValue=""><option value="" disabled>选择系统</option>{systems.filter(canEdit).map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}</select></label>
            <label>标题<input name="title" required maxLength={160} /></label>
            <label>来源地址或文档编号<input name="source_url" maxLength={1000} /></label>
            <label>内容<textarea name="content" required maxLength={30000} rows={5} /></label>
            <button disabled={busy}>保存知识</button>
          </form>
        </section>
      )}

      <div className="section-heading"><h2>统一指标口径</h2><span>{terms.length} 项</span></div>
      {terms.map((term) => (
        <section className="panel" key={term.id}>
          <h3>{term.name} {term.unit && `（${term.unit}）`}</h3>
          <p>{term.definition}</p>
          <p className="hint">已关联：{term.metrics.map((m) => `${sources.find((s) => s.id === m.source_id)?.name || m.source_id} / ${m.name}`).join("、") || "尚无指标"}</p>
          <form className="platform-form" onSubmit={(event) => {
            event.preventDefault();
            const values = new FormData(event.currentTarget);
            act(async () => {
              await api(`/platform/metric-terms/${term.id}/metrics/${values.get("metric_id")}`, {});
            });
          }}>
            <label>关联现有指标<select name="metric_id" required defaultValue="">
              <option value="" disabled>选择可管理的指标</option>
              {sources.filter((s) => s.my_role === "owner").flatMap((s) => s.metrics.map((m) =>
                <option key={m.id} value={m.id}>{s.name} / {m.name}</option>))}
            </select></label>
            <button disabled={busy}>关联</button>
          </form>
        </section>
      ))}
      {user.role === "admin" && (
        <section className="panel">
          <h3>创建统一指标口径</h3>
          <form className="platform-form" onSubmit={(event) => {
            event.preventDefault();
            const form = event.currentTarget;
            const values = new FormData(form);
            act(async () => {
              await api("/platform/metric-terms", {
                name: values.get("name"), definition: values.get("definition"), unit: values.get("unit"),
              });
              form.reset();
            });
          }}>
            <label>名称<input name="name" required maxLength={100} /></label>
            <label>单位<input name="unit" maxLength={40} /></label>
            <label>统一定义<textarea name="definition" required maxLength={3000} rows={3} /></label>
            <button className="primary" disabled={busy}>创建口径</button>
          </form>
        </section>
      )}
    </>
  );
}

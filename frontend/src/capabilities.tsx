import { useEffect, useState } from "react";
import { api } from "./session";

type Kind = "development" | "analysis";
type Approval = {
  version: number;
  ready: boolean;
  resources: {
    resource_id: string;
    name: string;
    approved: boolean;
    approved_by_name?: string;
    can_approve: boolean;
  }[];
};
export function ResourceApprovals({
  kind,
  id,
  version,
  status,
  dirty = false,
  onChanged,
}: {
  kind: Kind;
  id: string;
  version: number;
  status: string;
  dirty?: boolean;
  onChanged: () => Promise<void>;
}) {
  const [data, setData] = useState<Approval | null>(null),
    [error, setError] = useState(""),
    [busy, setBusy] = useState(false);
  const load = () =>
    api(`/orchestration/${kind}/${id}/approvals`).then(setData);
  useEffect(() => {
    setError("");
    load().catch((e) => setError(e.message));
  }, [kind, id, version, status]);
  return (
    <section className="panel">
      <h2>各系统负责人审批</h2>
      <p className="hint">
        各负责人批准自己负责的资源，全部通过后进入执行。修改方案会废弃已有批准。
      </p>
      {error && (
        <p className="error" role="alert">
          {error}{" "}
          <button
            disabled={busy}
            onClick={() =>
              load()
                .then(() => setError(""))
                .catch((e) => setError(e.message))
            }
          >
            重新加载
          </button>
        </p>
      )}
      {!data && !error && <p className="hint">正在读取审批状态…</p>}
      {data && (
        <>
          <div className="approval-list">
            {data.resources.map((r) => (
              <div className="approval-row" key={r.resource_id}>
                <div>
                  <strong>{r.name}</strong>
                  <small>
                    {r.approved
                      ? `已批准 · ${r.approved_by_name || "负责人"}`
                      : "等待负责人批准"}
                  </small>
                </div>
                <span
                  className={`badge ${r.approved ? "completed" : "awaiting_approval"}`}
                >
                  {r.approved ? "已批准" : "待批准"}
                </span>
                {r.can_approve &&
                  !r.approved &&
                  status === "awaiting_approval" && (
                    <button
                      className="primary"
                      disabled={busy || dirty}
                      onClick={async () => {
                        setBusy(true);
                        setError("");
                        try {
                          await api(`/orchestration/${kind}/${id}/approve`, {
                            version: data.version,
                            resource_ids: [r.resource_id],
                          });
                          await load();
                          await onChanged();
                        } catch (e) {
                          setError((e as Error).message);
                        } finally {
                          setBusy(false);
                        }
                      }}
                    >
                      批准此资源
                    </button>
                  )}
              </div>
            ))}
          </div>
          {dirty && <p className="hint">请先保存修改，再批准当前版本。</p>}
          {data.ready && <p className="hint">当前版本的资源审批已通过。</p>}
        </>
      )}
    </section>
  );
}
export function RetryTask({
  kind,
  id,
  status,
  canEdit,
  onChanged,
}: {
  kind: Kind;
  id: string;
  status: string;
  canEdit?: boolean;
  onChanged: () => Promise<void>;
}) {
  const [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  if (!canEdit || !["failed", "interrupted"].includes(status)) return null;
  return (
    <section className="panel">
      <h2>恢复任务</h2>
      <p className="hint">
        重试会重新检查权限、方案版本和审批。已完成的外部操作请先确认，具体恢复方式以执行记录为准。
      </p>
      <button
        disabled={busy}
        onClick={async () => {
          setBusy(true);
          setError("");
          try {
            await api(`/orchestration/${kind}/${id}/retry`, {});
            await onChanged();
          } catch (e) {
            setError((e as Error).message);
          } finally {
            setBusy(false);
          }
        }}
      >
        {busy ? "正在恢复…" : "重试任务"}
      </button>
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
    </section>
  );
}

type ProjectPlan = {
  project_id: string;
  impact: string;
  allowed_paths: string[];
  validation: string[];
  rollback: string;
  depends_on: string[];
  review_tests?: boolean;
};
type StructuredPlan = {
  objective: string;
  feasibility: "feasible" | "needs_information" | "infeasible";
  questions: string[];
  risks: string[];
  projects: ProjectPlan[];
};
export function DevelopmentPlan({
  id,
  version,
  status,
  canEdit,
  projects,
  onChanged,
  outerDirty = false,
}: {
  id: string;
  version: number;
  status: string;
  canEdit: boolean;
  projects: { id: string; name: string }[];
  outerDirty?: boolean;
  onChanged: () => Promise<void>;
}) {
  const [plan, setPlan] = useState<StructuredPlan | null>(null),
    [initial, setInitial] = useState(""),
    [warnings, setWarnings] = useState<string[]>([]),
    [error, setError] = useState(""),
    [busy, setBusy] = useState(false);
  useEffect(() => {
    setPlan(null);
    setError("");
    api(`/development/tasks/${id}/plan`)
      .then((data) => {
        const p = data.plan || data.structured_plan;
        const template: StructuredPlan = {
          objective: "",
          feasibility: "needs_information",
          questions: [],
          risks: [],
          projects: (data.projects || []).map((x: { project_id: string }) => ({
            project_id: x.project_id,
            impact: "",
            allowed_paths: [],
            validation: [],
            rollback: "",
            depends_on: [],
            review_tests: false,
          })),
        };
        setPlan(p || (status === "awaiting_approval" ? template : null));
        setInitial(JSON.stringify(p));
        setWarnings(data.validation_errors || data.warnings || []);
      })
      .catch((e) => setError(e.message));
  }, [id, version]);
  const edit = canEdit && status === "awaiting_approval";
  const dirty = Boolean(plan && JSON.stringify(plan) !== initial);
  const update = (patch: Partial<StructuredPlan>) =>
    plan && setPlan({ ...plan, ...patch });
  const updateProject = (index: number, patch: Partial<ProjectPlan>) =>
    plan &&
    update({
      projects: plan.projects.map((p, i) =>
        i === index ? { ...p, ...patch } : p,
      ),
    });
  return (
    <>
      <section className="panel">
        <div className="section-heading">
          <h2>跨系统实施计划</h2>
          <span>版本 {version}</span>
        </div>
        <p className="hint">
          明确可行性、每个系统的影响、允许修改的范围，以及验证和回退方式。
        </p>
        {error && (
          <p className="error" role="alert">
            {error}
          </p>
        )}
        {!plan && !error && (
          <p className="hint">
            正在读取实施计划；尚未生成的计划将在调研完成后出现。
          </p>
        )}
        {warnings.map((w, i) => (
          <p className="warning-note" key={i}>
            {w}
          </p>
        ))}
        {plan && (
          <form
            onSubmit={async (e) => {
              e.preventDefault();
              setBusy(true);
              setError("");
              try {
                const cleanList = (items: string[]) =>
                  items.map((x) => x.trim()).filter(Boolean);
                const normalized = {
                  ...plan,
                  questions: cleanList(plan.questions),
                  risks: cleanList(plan.risks),
                  projects: plan.projects.map((p) => ({
                    ...p,
                    allowed_paths: cleanList(p.allowed_paths),
                    validation: cleanList(
                      Array.isArray(p.validation)
                        ? p.validation
                        : [p.validation],
                    ),
                  })),
                };
                await api(`/development/tasks/${id}/plan`, {
                  version,
                  plan: normalized,
                });
                await onChanged();
                setInitial(JSON.stringify(plan));
              } catch (e) {
                setError((e as Error).message);
              } finally {
                setBusy(false);
              }
            }}
          >
            <fieldset disabled={!edit || busy} className="capability-fields">
              <label>
                业务目标
                <textarea
                  value={plan.objective}
                  onChange={(e) => update({ objective: e.target.value })}
                  required
                  rows={2}
                />
              </label>
              <label>
                可行性结论
                <select
                  value={plan.feasibility}
                  onChange={(e) =>
                    update({
                      feasibility: e.target
                        .value as StructuredPlan["feasibility"],
                    })
                  }
                >
                  <option value="feasible">可实施</option>
                  <option value="needs_information">需要补充信息</option>
                  <option value="infeasible">当前不可实施</option>
                </select>
              </label>
              <div className="field-grid">
                <label>
                  待确认事项（每行一项）
                  <textarea
                    value={plan.questions.join("\n")}
                    onChange={(e) =>
                      update({ questions: e.target.value.split("\n") })
                    }
                    rows={3}
                  />
                </label>
                <label>
                  风险（每行一项）
                  <textarea
                    value={plan.risks.join("\n")}
                    onChange={(e) =>
                      update({ risks: e.target.value.split("\n") })
                    }
                    rows={3}
                  />
                </label>
              </div>
              {plan.projects.map((p, i) => (
                <div className="project-plan" key={p.project_id}>
                  <h3>
                    {projects.find((x) => x.id === p.project_id)?.name ||
                      p.project_id}
                  </h3>
                  <label>
                    系统影响
                    <textarea
                      required
                      value={p.impact}
                      onChange={(e) =>
                        updateProject(i, { impact: e.target.value })
                      }
                      rows={2}
                    />
                  </label>
                  <label>
                    允许修改的路径（每行一项）
                    <textarea
                      required
                      value={p.allowed_paths.join("\n")}
                      onChange={(e) =>
                        updateProject(i, {
                          allowed_paths: e.target.value.split("\n"),
                        })
                      }
                      placeholder="例如 src/orders/"
                      rows={2}
                    />
                  </label>
                  <div className="field-grid">
                    <label>
                      验证方法
                      <textarea
                        required
                        value={
                          Array.isArray(p.validation)
                            ? p.validation.join("\n")
                            : p.validation
                        }
                        onChange={(e) =>
                          updateProject(i, {
                            validation: e.target.value.split("\n"),
                          })
                        }
                        rows={3}
                      />
                    </label>
                    <label>
                      回退方案
                      <textarea
                        required
                        value={p.rollback}
                        onChange={(e) =>
                          updateProject(i, { rollback: e.target.value })
                        }
                        rows={3}
                      />
                    </label>
                  </div>
                  <label className="checkbox-label">
                    <input
                      type="checkbox"
                      checked={Boolean(p.review_tests)}
                      onChange={(e) =>
                        updateProject(i, { review_tests: e.target.checked })
                      }
                    />
                    允许修改现有测试（需负责人批准）
                  </label>
                  <fieldset className="source-picker">
                    <legend>先完成哪些系统</legend>
                    {plan.projects
                      .filter((x) => x.project_id !== p.project_id)
                      .map((other) => (
                        <label key={other.project_id}>
                          <input
                            type="checkbox"
                            checked={p.depends_on.includes(other.project_id)}
                            onChange={(e) =>
                              updateProject(i, {
                                depends_on: e.target.checked
                                  ? [...p.depends_on, other.project_id]
                                  : p.depends_on.filter(
                                      (x) => x !== other.project_id,
                                    ),
                              })
                            }
                          />
                          {projects.find((x) => x.id === other.project_id)
                            ?.name || other.project_id}
                        </label>
                      ))}
                    {plan.projects.length === 1 && (
                      <p className="hint">单系统任务，无前置系统。</p>
                    )}
                  </fieldset>
                </div>
              ))}
            </fieldset>
            {edit && (
              <button className="primary" disabled={busy || !dirty}>
                {busy ? "正在保存…" : "保存实施计划"}
              </button>
            )}
            {plan.feasibility !== "feasible" && (
              <p className="warning-note">
                该方案尚不能执行，请先解决可行性或信息缺口。
              </p>
            )}
          </form>
        )}
      </section>
      <ResourceApprovals
        kind="development"
        id={id}
        version={version}
        status={status}
        dirty={dirty || outerDirty}
        onChanged={onChanged}
      />
    </>
  );
}

type Brief = {
  objective: string;
  decision: string;
  constraints: string;
  assumptions: string;
};
export function AnalysisBrief({
  id,
  version,
  status,
  canEdit,
  onChanged,
  onDirtyChanged,
}: {
  id: string;
  version: number;
  status: string;
  canEdit?: boolean;
  onChanged: () => Promise<void>;
  onDirtyChanged?: (dirty: boolean) => void;
}) {
  const [brief, setBrief] = useState<Brief | null>(null),
    [error, setError] = useState(""),
    [busy, setBusy] = useState(false),
    [initial, setInitial] = useState("");
  useEffect(() => {
    setError("");
    api(`/analysis/jobs/${id}/brief`)
      .then((r) => {
        setBrief(r.brief);
        setInitial(JSON.stringify(r.brief));
      })
      .catch((e) => setError(e.message));
  }, [id, version]);
  const edit = canEdit && status === "awaiting_approval";
  useEffect(() => {
    onDirtyChanged?.(Boolean(brief && initial !== JSON.stringify(brief)));
  }, [brief, initial, onDirtyChanged]);
  return (
    <section className="panel">
      <h2>决策目标与边界</h2>
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      {!brief && !error && <p className="hint">正在读取分析说明…</p>}
      {brief && (
        <form
          onSubmit={async (e) => {
            e.preventDefault();
            setBusy(true);
            setError("");
            try {
              await api(`/analysis/jobs/${id}/brief`, { version, brief });
              await onChanged();
              setInitial(JSON.stringify(brief));
            } catch (e) {
              setError((e as Error).message);
            } finally {
              setBusy(false);
            }
          }}
        >
          <fieldset className="capability-fields" disabled={!edit || busy}>
            {(
              [
                ["objective", "分析目标"],
                ["decision", "需要支持的决策"],
                ["constraints", "约束条件"],
                ["assumptions", "业务假设"],
              ] as const
            ).map(([key, label]) => (
              <label key={key}>
                {label}
                <textarea
                  rows={2}
                  value={brief[key] || ""}
                  onChange={(e) =>
                    setBrief({ ...brief, [key]: e.target.value })
                  }
                />
              </label>
            ))}
          </fieldset>
          {edit && (
            <button disabled={busy || initial === JSON.stringify(brief)}>
              保存决策说明
            </button>
          )}
        </form>
      )}
    </section>
  );
}
export function EvidencePanel({
  kind,
  id,
  status,
}: {
  kind: Kind;
  id: string;
  status: string;
}) {
  const [data, setData] = useState<Record<string, unknown> | null>(null),
    [error, setError] = useState("");
  useEffect(() => {
    setData(null);
    setError("");
    api(
      kind === "analysis"
        ? `/analysis/jobs/${id}/evidence`
        : `/development/tasks/${id}/delivery`,
    )
      .then(setData)
      .catch((e) => setError(e.message));
  }, [kind, id, status]);
  return (
    <section className="panel">
      <h2>{kind === "analysis" ? "数据来源与计算证据" : "跨系统交付记录"}</h2>
      <p className="hint">
        {kind === "analysis"
          ? "核对采集时间、指标口径和计算范围；分析判断应结合数据局限，不能将相关性当作因果。"
          : "逐系统核对执行结果、验证和回退信息，最终业务验收由负责人确认。"}
      </p>
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      {!data && !error && <p className="hint">正在读取记录…</p>}
      {data && <EvidenceValue value={data} />}
    </section>
  );
}
const labels: Record<string, string> = {
  sources: "数据来源",
  metrics: "指标口径",
  rows: "聚合结果",
  query_spec: "计算计划",
  generated_at: "生成时间",
  captured_at: "采集时间",
  collected_at: "采集时间",
  frozen_at: "冻结时间",
  warnings: "注意事项",
  limitations: "数据局限",
  comparisons: "比较分析",
  scenarios: "情景计算",
  projects: "系统交付",
  checks: "验证结果",
  status: "状态",
  summary: "说明",
  name: "名称",
  source_name: "数据源",
  metric_name: "指标",
  version: "版本",
  approved_version: "批准版本",
  project_id: "仓库",
  project_name: "系统",
  base: "基线",
  worktree: "工作目录",
  complete: "完整性",
  row_count: "结果数量",
  evidence: "证据",
  snapshot: "批准时快照",
  plan: "实施计划",
  result: "结果",
  delivery: "交付",
  last_collected_at: "最近采集时间",
  definition: "指标定义",
  unit: "单位",
  aggregation: "聚合方法",
  quality: "质量检查",
  calculations: "比较与情景结果",
  claims: "报告结论",
  claim_validation: "结论证据校验",
  started_at: "开始采集",
  configuration_hash: "配置校验值",
  definition_hash: "定义校验值",
  scanned_rows: "读取记录数",
  included_rows: "纳入记录数",
  invalid_numeric_rows: "无效数值数",
  invalid_date_rows: "无效日期数",
  null_dimension_rows: "空维度记录数",
  group_count: "分组数",
  input_fingerprint: "数据校验值",
  stage: "阶段",
  changed_files: "变更文件",
  handoff: "交接说明",
  instruction: "交接指引",
  branch: "分支",
  pr_url: "PR链接",
  ci_url: "CI链接",
  error: "错误",
  updated: "更新时间",
  task_id: "任务",
  value: "指标值",
  baseline: "基线",
  target: "目标",
  change: "变化值",
  target_met: "是否达到目标",
  measured_at: "复测时间",
  detail: "说明",
  operation: "计算方法",
  kind: "类型",
  evidence_id: "证据编号",
  evidence_ids: "证据编号",
  dimensions: "维度",
  command: "验证命令",
  exit_code: "退出状态",
  output: "验证输出",
  diff: "代码差异",
  diff_truncated: "差异是否截断",
  allowed_paths: "允许修改的路径",
  validation: "验证方法",
  rollback: "回退方案",
  depends_on: "前置系统",
  review_tests: "允许修改现有测试",
};
export function EvidenceValue({ value }: { value: unknown }) {
  if (value === null || value === undefined)
    return <span className="hint">未记录</span>;
  if (typeof value !== "object")
    return (
      <span className="preserve">
        {typeof value === "boolean" ? (value ? "是" : "否") : String(value)}
      </span>
    );
  if (Array.isArray(value))
    return value.length ? (
      <ul className="evidence-values">
        {value.map((v, i) => (
          <li key={i}>
            <EvidenceValue value={v} />
          </li>
        ))}
      </ul>
    ) : (
      <span className="hint">暂无记录</span>
    );
  return (
    <dl className="evidence-details">
      {Object.entries(value).map(([k, v]) => (
        <div key={k}>
          <dt>{labels[k] || k}</dt>
          <dd>
            {[
              "rows",
              "query_spec",
              "approval_snapshot",
              "snapshot",
              "metrics",
            ].includes(k) ? (
              <details>
                <summary>展开记录</summary>
                <EvidenceValue value={v} />
              </details>
            ) : typeof v === "number" && /(_at|timestamp)$/.test(k) ? (
              new Date(v * 1000).toLocaleString()
            ) : (
              <EvidenceValue value={v} />
            )}
          </dd>
        </div>
      ))}
    </dl>
  );
}

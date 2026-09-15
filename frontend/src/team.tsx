import { useEffect, useState } from "react";
import { Plus, Users, X } from "lucide-react";
import { api, type User } from "./session";

const roleNames: Record<string, string> = {
  admin: "系统管理员",
  member: "普通成员",
  maintainer: "项目负责人",
  developer: "开发者",
  viewer: "只读成员",
};
export { roleNames };

export function TeamAdmin({ current }: { current: User }) {
  const [users, setUsers] = useState<User[]>([]),
    [error, setError] = useState(""),
    [busy, setBusy] = useState(false),
    [modal, setModal] = useState(""),
    [target, setTarget] = useState<User | null>(null);
  const load = () => api("/users").then(setUsers);
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
  return (
    <>
      <div className="eyebrow">PEOPLE & ACCESS</div>
      <div className="page-title">
        <div>
          <h1>团队与账号</h1>
          <p>管理员创建账号，再通过项目成员分配访问范围。</p>
        </div>
        <button className="primary" onClick={() => setModal("new")}>
          <Plus size={16} />
          创建账号
        </button>
      </div>
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      <div className="panel table-wrap">
        <table className="team-table">
          <thead>
            <tr>
              <th>成员</th>
              <th>系统角色</th>
              <th>状态</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            {users.map((u) => (
              <tr key={u.id}>
                <td>
                  <strong>{u.display_name}</strong>
                  <small>
                    {u.username}
                    {u.id === current.id ? " · 当前账号" : ""}
                  </small>
                </td>
                <td>{roleNames[u.role]}</td>
                <td>
                  <span
                    className={"badge " + (u.active ? "completed" : "failed")}
                  >
                    {u.active ? "启用" : "停用"}
                  </span>
                  {u.must_change_password ? (
                    <small>首次登录需改密</small>
                  ) : null}
                </td>
                <td>
                  <div className="compact-actions">
                    <button
                      disabled={busy || u.id === current.id}
                      onClick={() => {
                        setTarget(u);
                        setModal("edit");
                      }}
                    >
                      管理
                    </button>
                    <button
                      disabled={busy || u.id === current.id}
                      onClick={() => {
                        setTarget(u);
                        setModal("password");
                      }}
                    >
                      重置密码
                    </button>
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="hint">
        系统管理员可以访问所有项目。普通成员仅能访问被分配的项目；变更系统角色、停用或重置密码会撤销该用户已有会话。
      </p>
      {modal && (
        <div className="overlay">
          <section
            className="modal"
            role="dialog"
            aria-modal="true"
            aria-label="账号管理"
          >
            <div className="section-heading">
              <h2>
                {modal === "new"
                  ? "创建团队账号"
                  : modal === "edit"
                    ? "管理 " + target?.display_name
                    : "重置 " + target?.display_name + " 的密码"}
              </h2>
              <button
                onClick={() => setModal("")}
                disabled={busy}
                aria-label="关闭"
              >
                <X size={18} />
              </button>
            </div>
            <form
              onSubmit={(e) => {
                e.preventDefault();
                const f = new FormData(e.currentTarget);
                act(async () => {
                  if (modal === "new")
                    await api("/users", {
                      username: f.get("username"),
                      display_name: f.get("display_name"),
                      password: f.get("password"),
                      role: f.get("role"),
                    });
                  else if (modal === "edit")
                    await api("/users/" + target!.id, {
                      role: f.get("role"),
                      active: f.get("active") === "1",
                    });
                  else
                    await api("/users/" + target!.id + "/password", {
                      password: f.get("password"),
                    });
                  setModal("");
                });
              }}
            >
              {modal === "new" && (
                <>
                  <label>
                    用户名
                    <input
                      name="username"
                      required
                      pattern="[a-zA-Z0-9_.-]{3,64}"
                      placeholder="3–64 位字母、数字、点、横线或下划线"
                    />
                  </label>
                  <label>
                    显示名称
                    <input name="display_name" required maxLength={80} />
                  </label>
                </>
              )}
              {modal !== "password" && (
                <label>
                  系统角色
                  <select
                    name="role"
                    defaultValue={
                      target && modal === "edit" ? target.role : "member"
                    }
                  >
                    <option value="member">普通成员</option>
                    <option value="admin">系统管理员（所有项目）</option>
                  </select>
                </label>
              )}
              {modal === "edit" ? (
                <label>
                  账号状态
                  <select name="active" defaultValue={String(target!.active)}>
                    <option value="1">启用</option>
                    <option value="0">停用</option>
                  </select>
                </label>
              ) : (
                <>
                  <label>
                    临时密码
                    <input
                      type="password"
                      name="password"
                      required
                      minLength={12}
                      maxLength={128}
                      autoComplete="new-password"
                    />
                  </label>
                  <p className="hint">
                    请通过企业内部安全渠道交给本人；用户首次登录必须修改密码。
                  </p>
                </>
              )}
              {error && <p className="error">{error}</p>}
              <button className="primary submit" disabled={busy}>
                确认保存
              </button>
            </form>
          </section>
        </div>
      )}
    </>
  );
}

type DirectoryUser = { id: string; username: string; display_name: string };
type Membership = DirectoryUser & {
  user_id: string;
  role: string;
  active: number;
};
export function ProjectMembers({
  project,
  onClose,
}: {
  project: { id: string; name: string };
  onClose: () => void;
}) {
  const [members, setMembers] = useState<Membership[]>([]),
    [directory, setDirectory] = useState<DirectoryUser[]>([]),
    [error, setError] = useState(""),
    [busy, setBusy] = useState(false);
  const load = async () => {
    const [m, d] = await Promise.all([
      api("/projects/" + project.id + "/members"),
      api("/directory"),
    ]);
    setMembers(m);
    setDirectory(d);
  };
  useEffect(() => {
    load().catch((e) => setError(e.message));
  }, [project.id]);
  const save = async (uid: string, role: string) => {
    setBusy(true);
    setError("");
    try {
      await api("/projects/" + project.id + "/members", { user_id: uid, role });
      await load();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="overlay">
      <section
        className="modal members-modal"
        role="dialog"
        aria-modal="true"
        aria-label="项目成员"
      >
        <div className="section-heading">
          <h2>
            <Users size={18} />
            {project.name} · 项目成员
          </h2>
          <button onClick={onClose} aria-label="关闭">
            <X size={18} />
          </button>
        </div>
        <p className="hint">
          负责人可审核和管理成员；开发者可创建任务并修改自己的方案；只读成员可查看项目内任务。
        </p>
        {error && (
          <p className="error" role="alert">
            {error}
          </p>
        )}
        <div className="member-list">
          {members.map((m) => (
            <form
              key={m.user_id}
              className="member-row"
              onSubmit={(e) => {
                e.preventDefault();
                save(
                  m.user_id,
                  String(new FormData(e.currentTarget).get("role")),
                );
              }}
            >
              <div>
                <strong>{m.display_name}</strong>
                <small>
                  {m.username}
                  {m.active ? "" : " · 已停用"}
                </small>
              </div>
              <select
                name="role"
                aria-label={m.display_name + " 的项目角色"}
                defaultValue={m.role}
              >
                <option value="maintainer">项目负责人</option>
                <option value="developer">开发者</option>
                <option value="viewer">只读成员</option>
                <option value="remove">移出项目</option>
              </select>
              <button disabled={busy}>保存</button>
            </form>
          ))}
        </div>
        <form
          className="add-member"
          onSubmit={(e) => {
            e.preventDefault();
            const f = new FormData(e.currentTarget);
            save(String(f.get("user_id")), String(f.get("role")));
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
                .filter((u) => !members.some((m) => m.user_id === u.id))
                .map((u) => (
                  <option key={u.id} value={u.id}>
                    {u.display_name} · {u.username}
                  </option>
                ))}
            </select>
          </label>
          <label>
            项目角色
            <select name="role" defaultValue="developer">
              <option value="developer">开发者</option>
              <option value="viewer">只读成员</option>
              <option value="maintainer">项目负责人</option>
            </select>
          </label>
          <button className="primary submit" disabled={busy}>
            添加到项目
          </button>
        </form>
      </section>
    </div>
  );
}

export function AuditLog() {
  const [rows, setRows] = useState<
      {
        id: number;
        actor_name: string;
        action: string;
        target: string;
        detail: string;
        created: number;
      }[]
    >([]),
    [error, setError] = useState("");
  useEffect(() => {
    api("/audit")
      .then(setRows)
      .catch((e) => setError(e.message));
  }, []);
  return (
    <>
      <div className="eyebrow">ACCOUNTABILITY</div>
      <h1>操作审计</h1>
      <p className="subtitle">最近 300 条账号、权限、任务与审批记录。</p>
      {error && <p className="error">{error}</p>}
      <section className="panel table-wrap">
        <table className="team-table">
          <thead>
            <tr>
              <th>时间</th>
              <th>操作者</th>
              <th>操作</th>
              <th>对象 / 详情</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.id}>
                <td>{new Date(r.created * 1000).toLocaleString()}</td>
                <td>{r.actor_name || "未认证 / 系统"}</td>
                <td>
                  <code>{r.action}</code>
                </td>
                <td>
                  {r.target}
                  <small>{r.detail}</small>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
    </>
  );
}

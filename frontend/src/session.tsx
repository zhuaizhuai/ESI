import { useEffect, useState } from "react";
import { Layers, LogIn, ShieldCheck } from "lucide-react";

export type User = {
  id: string;
  username: string;
  display_name: string;
  role: "admin" | "member";
  active: number;
  must_change_password: number;
};
let csrf = "";
export async function api(path: string, body?: unknown) {
  const r = await fetch("/api" + path, {
    credentials: "same-origin",
    ...(body === undefined
      ? {}
      : {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-ESI-Client": "workbench",
            "X-ESI-CSRF": csrf,
          },
          body: JSON.stringify(body),
        }),
  });
  const data = await r.json();
  if (!r.ok) {
    if (r.status === 401 && path !== "/auth/login")
      window.dispatchEvent(new Event("esi:logout"));
    throw new Error(
      typeof data.detail === "string"
        ? data.detail
        : JSON.stringify(data.detail),
    );
  }
  if (data.csrf) csrf = data.csrf;
  return data;
}

export function PasswordForm({
  onChanged,
  onCancel,
  forced = false,
}: {
  onChanged: (u: User) => void;
  onCancel?: () => void;
  forced?: boolean;
}) {
  const [error, setError] = useState(""),
    [busy, setBusy] = useState(false);
  return (
    <section className="auth-card">
      <ShieldCheck className="auth-symbol" size={32} />
      <h1>{forced ? "设置你的个人密码" : "修改密码"}</h1>
      <p>新密码至少 12 个字符。修改后，其他设备的登录将失效。</p>
      <form
        onSubmit={async (e) => {
          e.preventDefault();
          const f = new FormData(e.currentTarget);
          if (f.get("new_password") !== f.get("confirm")) {
            setError("两次新密码不一致");
            return;
          }
          setBusy(true);
          setError("");
          try {
            const result = await api("/auth/password", {
              current_password: f.get("current_password"),
              new_password: f.get("new_password"),
            });
            onChanged(result.user);
          } catch (e) {
            setError((e as Error).message);
          } finally {
            setBusy(false);
          }
        }}
      >
        <label>
          当前密码
          <input
            type="password"
            name="current_password"
            required
            autoComplete="current-password"
            maxLength={128}
          />
        </label>
        <label>
          新密码
          <input
            type="password"
            name="new_password"
            required
            autoComplete="new-password"
            minLength={12}
            maxLength={128}
          />
        </label>
        <label>
          确认新密码
          <input
            type="password"
            name="confirm"
            required
            autoComplete="new-password"
            minLength={12}
            maxLength={128}
          />
        </label>
        {error && (
          <p className="error" role="alert">
            {error}
          </p>
        )}
        <button className="primary submit" disabled={busy}>
          保存新密码
        </button>
        {onCancel && (
          <button type="button" className="submit" onClick={onCancel}>
            取消
          </button>
        )}
      </form>
    </section>
  );
}

export function SessionGate({
  children,
}: {
  children: (
    user: User,
    logout: () => Promise<void>,
    updateUser: (user: User) => void,
  ) => React.ReactNode;
}) {
  const [user, setUser] = useState<User | null>(null),
    [loading, setLoading] = useState(true),
    [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  useEffect(() => {
    let live = true;
    api("/auth/me")
      .then((r) => {
        if (live) setUser(r.user);
      })
      .catch(() => {})
      .finally(() => {
        if (live) setLoading(false);
      });
    const out = () => {
      csrf = "";
      setUser(null);
    };
    window.addEventListener("esi:logout", out);
    return () => {
      live = false;
      window.removeEventListener("esi:logout", out);
    };
  }, []);
  const logout = async () => {
    try {
      await api("/auth/logout", {});
    } finally {
      csrf = "";
      setUser(null);
    }
  };
  if (loading)
    return (
      <div className="auth-shell">
        <p>正在检查登录状态…</p>
      </div>
    );
  if (user?.must_change_password)
    return (
      <div className="auth-shell">
        <PasswordForm forced onChanged={setUser} />
        <button onClick={logout}>退出登录</button>
      </div>
    );
  if (user) return <>{children(user, logout, setUser)}</>;
  return (
    <div className="auth-shell">
      <div className="login-story">
        <div className="login-brand">
          <Layers size={30} /> ESI
        </div>
        <div className="eyebrow">ENTERPRISE SUPER INTELLIGENCE</div>
        <h1>
          连接企业数据，
          <br />
          让智能成为行动。
        </h1>
        <p>
          数据、分析、研发与审批，
          <br />
          在一个受控且可追溯的工作空间中协作。
        </p>
        <div className="login-features">
          <span>企业多用户权限</span>
          <span>人工审批</span>
          <span>全程可追溯</span>
        </div>
      </div>
      <section className="auth-card">
        <LogIn size={28} className="auth-symbol" />
        <h1>登录企业工作空间</h1>
        <p>使用管理员为你创建的账号登录。</p>
        <form
          onSubmit={async (e) => {
            e.preventDefault();
            const f = new FormData(e.currentTarget);
            setBusy(true);
            setError("");
            try {
              const r = await api("/auth/login", {
                username: f.get("username"),
                password: f.get("password"),
              });
              setUser(r.user);
            } catch (e) {
              setError((e as Error).message);
            } finally {
              setBusy(false);
            }
          }}
        >
          <label>
            用户名
            <input
              autoFocus
              name="username"
              required
              autoComplete="username"
              maxLength={80}
              placeholder="输入企业账号"
            />
          </label>
          <label>
            密码
            <input
              type="password"
              name="password"
              required
              autoComplete="current-password"
              maxLength={128}
              placeholder="输入密码"
            />
          </label>
          {error && (
            <p className="error" role="alert">
              {error}
            </p>
          )}
          <button className="primary submit" disabled={busy}>
            {busy ? "正在登录…" : "登录工作空间"}
            <LogIn size={16} />
          </button>
        </form>
        <p className="hint">
          首次部署请由管理员在服务器初始化账号。系统不提供公开注册。
        </p>
      </section>
    </div>
  );
}

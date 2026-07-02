import { useEffect, useState } from "react";
import { Radar, KeyRound } from "lucide-react";
import { Button, Field, Input, ErrorNote } from "../components/form";
import { api, setToken } from "../lib/api";

// Sign-in gate. On first run (no users) it shows "create admin"; afterwards a
// normal username/password login. An advanced section still accepts the raw
// API token for machine/bootstrap access.
export default function Login({ onAuthed }) {
  const [usersExist, setUsersExist] = useState(true);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [err, setErr] = useState(null);
  const [busy, setBusy] = useState(false);
  const [showToken, setShowToken] = useState(false);
  const [token, setTok] = useState("");

  useEffect(() => {
    api.authStatus().then(s => setUsersExist(!!s.users_exist)).catch(() => {});
  }, []);

  const submit = async () => {
    setErr(null); setBusy(true);
    try {
      const res = usersExist ? await api.login(username, password)
                             : await api.register(username, password);
      setToken(res.token);
      onAuthed();
    } catch (e) { setErr(e.message); }
    finally { setBusy(false); }
  };

  const useTokenDirect = () => {
    if (!token.trim()) return;
    setToken(token.trim());
    onAuthed();
  };

  return (
    <div className="min-h-screen grid place-items-center bg-panel2 p-4">
      <div className="w-full max-w-sm card p-6 space-y-4">
        <div className="flex items-center gap-2.5">
          <Radar className="w-6 h-6 text-beacon" />
          <div>
            <div className="font-semibold tracking-tight leading-none">Beacon</div>
            <div className="text-[10px] uppercase tracking-[0.2em] text-muted mt-1">Trader</div>
          </div>
        </div>
        <div className="text-sm font-medium">
          {usersExist ? "Sign in" : "Create your admin account"}
        </div>
        <ErrorNote>{err}</ErrorNote>
        <Field label="Username">
          <Input value={username} onChange={e => setUsername(e.target.value)}
                 onKeyDown={e => e.key === "Enter" && submit()} autoFocus />
        </Field>
        <Field label="Password" hint={usersExist ? null : "at least 8 characters"}>
          <Input type="password" value={password} onChange={e => setPassword(e.target.value)}
                 onKeyDown={e => e.key === "Enter" && submit()} />
        </Field>
        <Button onClick={submit} className="w-full justify-center" disabled={busy}>
          {busy ? "…" : usersExist ? "Sign in" : "Create account"}
        </Button>

        <button className="text-[11px] text-muted hover:text-ink flex items-center gap-1"
                onClick={() => setShowToken(v => !v)}>
          <KeyRound className="w-3 h-3" /> Advanced: use API token
        </button>
        {showToken && (
          <div className="space-y-2">
            <Input type="password" value={token} onChange={e => setTok(e.target.value)}
                   placeholder="paste API_TOKEN" />
            <Button variant="ghost" onClick={useTokenDirect} className="w-full justify-center">Use token</Button>
          </div>
        )}
      </div>
    </div>
  );
}

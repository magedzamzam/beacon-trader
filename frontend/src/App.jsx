import { useEffect, useState } from "react";
import Layout from "./components/Layout";
import Login from "./pages/Login";
import { api, getToken, clearToken } from "./lib/api";
import { PAGES, REDIRECTS } from "./lib/nav";

export default function App() {
  const [view, setView] = useState("dashboard");
  const [authed, setAuthed] = useState(false);
  const [checking, setChecking] = useState(true);
  // Global account filter — "" means "all accounts". Persisted across reloads.
  const [account, setAccount] = useState(() => localStorage.getItem("beacon_account") || "");
  const [accounts, setAccounts] = useState([]);

  useEffect(() => {
    (async () => {
      if (!getToken()) { setChecking(false); return; }
      try { await api.me(); setAuthed(true); }
      catch { clearToken(); }
      finally { setChecking(false); }
    })();
  }, []);

  useEffect(() => {
    if (!authed) return;
    api.accounts().then(list => {
      setAccounts(list);
      // Drop a stale selection (e.g. the account was deleted).
      setAccount(a => (a && !list.some(x => String(x.id) === String(a)) ? "" : a));
    }).catch(() => {});
  }, [authed]);

  const chooseAccount = (id) => {
    setAccount(id);
    if (id) localStorage.setItem("beacon_account", id);
    else localStorage.removeItem("beacon_account");
  };

  if (checking) return null;
  if (!authed) return <Login onAuthed={() => setAuthed(true)} />;

  const activeView = REDIRECTS[view] || view;      // legacy id -> sensible leaf
  const Page = PAGES[activeView] || PAGES.dashboard;
  return (
    <Layout view={activeView} setView={setView}
      accounts={accounts} account={account} setAccount={chooseAccount}>
      <Page setView={setView} account={account} />
    </Layout>
  );
}

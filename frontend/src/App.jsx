import { useEffect, useState } from "react";
import Layout from "./components/Layout";
import Login from "./pages/Login";
import { api, getToken, clearToken } from "./lib/api";
import Dashboard from "./pages/Dashboard";
import Positions from "./pages/Positions";
import Signals from "./pages/Signals";
import History from "./pages/History";
import Performance from "./pages/Performance";
import Reconciliation from "./pages/Reconciliation";
import Sources from "./pages/Sources";
import Brokers from "./pages/Brokers";
import Symbols from "./pages/Symbols";
import Risk from "./pages/Risk";
import Chart from "./pages/Chart";
import Messages from "./pages/Messages";
import Activity from "./pages/Activity";
import AI from "./pages/AI";
import Configuration from "./pages/Configuration";

const PAGES = { dashboard: Dashboard, positions: Positions, signals: Signals,
  history: History, performance: Performance, reconciliation: Reconciliation,
  sources: Sources, brokers: Brokers, symbols: Symbols, risk: Risk, chart: Chart,
  messages: Messages, activity: Activity, ai: AI, configuration: Configuration };

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

  const Page = PAGES[view] || Dashboard;
  return (
    <Layout view={view} setView={setView}
      accounts={accounts} account={account} setAccount={chooseAccount}>
      <Page setView={setView} account={account} />
    </Layout>
  );
}

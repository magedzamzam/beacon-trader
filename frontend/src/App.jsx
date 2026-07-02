import { useState } from "react";
import Layout from "./components/Layout";
import Dashboard from "./pages/Dashboard";
import Positions from "./pages/Positions";
import Signals from "./pages/Signals";
import History from "./pages/History";
import Performance from "./pages/Performance";
import Sources from "./pages/Sources";
import Brokers from "./pages/Brokers";
import Symbols from "./pages/Symbols";
import Risk from "./pages/Risk";
import Chart from "./pages/Chart";
import Messages from "./pages/Messages";
import Activity from "./pages/Activity";
import AI from "./pages/AI";

const PAGES = { dashboard: Dashboard, positions: Positions, signals: Signals,
  history: History, performance: Performance, sources: Sources, brokers: Brokers,
  symbols: Symbols, risk: Risk, chart: Chart, messages: Messages,
  activity: Activity, ai: AI };

export default function App() {
  const [view, setView] = useState("dashboard");
  const Page = PAGES[view] || Dashboard;
  return <Layout view={view} setView={setView}><Page /></Layout>;
}

import { useState } from "react";
import Layout from "./components/Layout";
import Dashboard from "./pages/Dashboard";
import Positions from "./pages/Positions";
import Signals from "./pages/Signals";
import History from "./pages/History";
import Performance from "./pages/Performance";
import Sources from "./pages/Sources";
import Brokers from "./pages/Brokers";

const PAGES = { dashboard: Dashboard, positions: Positions, signals: Signals,
  history: History, performance: Performance, sources: Sources, brokers: Brokers };

export default function App() {
  const [view, setView] = useState("dashboard");
  const Page = PAGES[view] || Dashboard;
  return <Layout view={view} setView={setView}><Page /></Layout>;
}

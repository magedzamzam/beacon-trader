/**
 * Single source of truth for navigation + page routing (#57).
 *
 * `NAV` is the sidebar hierarchy: groups -> items, where a Settings item may have
 * `children` (an expandable subgroup). Every leaf carries {id, label, icon}.
 * `PAGES` maps each leaf id -> its component. Both the sidebar (Layout) and the
 * page router (App) consume these — there is no second menu.
 */
import {
  Activity, Radar, Radio, CandlestickChart, MessageSquare, GitBranch, ListChecks,
  Sigma, Brain, GitCompare, BarChart3, Building2, Rss, Coins, Plug, ShieldCheck,
  DollarSign, Clock, Sparkles, LineChart, Bell, Settings, Users, KeyRound,
  FileCheck, Database, CreditCard, BookOpen,
} from "lucide-react";

import Dashboard from "../pages/Dashboard";
import Positions from "../pages/Positions";
import Signals from "../pages/Signals";
import Chart from "../pages/Chart";
import Messages from "../pages/Messages";
import Activity_ from "../pages/Activity";
import History from "../pages/History";
import Analytics from "../pages/Analytics";
import Analysis from "../pages/Analysis";
import Reconciliation from "../pages/Reconciliation";
import Performance from "../pages/Performance";
import Brokers from "../pages/Brokers";
import Sources from "../pages/Sources";
import Symbols from "../pages/Symbols";
import Risk from "../pages/Risk";
import Strategies from "../pages/Strategies";
import Currency from "../pages/settings/Currency";
import TradingHours from "../pages/TradingHours";
import AI from "../pages/AI";
import Indicators from "../pages/Indicators";
import Notifications from "../pages/Notifications";
import SystemHealth from "../pages/SystemHealth";
import Help from "../pages/Help";
import { PLACEHOLDERS } from "../components/settings/placeholders";

export const NAV = [
  { title: "Overview", items: [
    { id: "dashboard", label: "Dashboard", icon: Activity },
    { id: "help", label: "Help & Glossary", icon: BookOpen },
  ]},
  { title: "Live Trading", items: [
    { id: "positions", label: "Positions", icon: Radar },
    { id: "signals", label: "Signals", icon: Radio },
    { id: "chart", label: "Chart", icon: CandlestickChart },
    { id: "messages", label: "Messages", icon: MessageSquare },
    { id: "activity", label: "Activity", icon: GitBranch },
    { id: "history", label: "History", icon: ListChecks },
  ]},
  { title: "Intelligence", items: [
    { id: "analytics", label: "Analytics", icon: Sigma },
    { id: "analysis", label: "Bayesian Analysis", icon: Brain },
    { id: "reconciliation", label: "Reconciler", icon: GitCompare },
    { id: "performance", label: "Performance", icon: BarChart3 },
  ]},
  { title: "Settings", items: [
    { label: "Connectivity", icon: Plug, children: [
      { id: "brokers", label: "Brokers & Accounts", icon: Building2 },
      { id: "sources", label: "Signal Sources", icon: Rss },
      { id: "symbols", label: "Symbols Mapping", icon: Coins },
      { id: "integrations", label: "Integrations", icon: Plug },
    ]},
    { label: "Trading", icon: GitBranch, children: [
      { id: "risk", label: "Risk & Limits", icon: ShieldCheck },
      { id: "currency", label: "Currency & FX", icon: DollarSign },
      { id: "strategies", label: "Strategies", icon: GitBranch },
      { id: "hours", label: "Trading Hours", icon: Clock },
    ]},
    { label: "Intelligence", icon: Sparkles, children: [
      { id: "ai", label: "AI Validation", icon: Sparkles },
      { id: "indicators", label: "Indicators", icon: LineChart },
      { id: "notifications", label: "Notifications", icon: Bell },
    ]},
    { label: "Platform", icon: Settings, children: [
      { id: "general", label: "General", icon: Settings },
      { id: "users", label: "Users & Roles", icon: Users },
      { id: "api", label: "API & Webhooks", icon: KeyRound },
      { id: "compliance", label: "Compliance & Audit", icon: FileCheck },
      { id: "backups", label: "Backups & Data", icon: Database },
      { id: "billing", label: "Billing & Usage", icon: CreditCard },
      { id: "system", label: "System Health", icon: Activity },
    ]},
  ]},
];

export const PAGES = {
  dashboard: Dashboard, help: Help,
  positions: Positions, signals: Signals, chart: Chart,
  messages: Messages, activity: Activity_, history: History,
  analytics: Analytics, analysis: Analysis, reconciliation: Reconciliation,
  performance: Performance,
  brokers: Brokers, sources: Sources, symbols: Symbols, integrations: PLACEHOLDERS.integrations,
  risk: Risk, currency: Currency, strategies: Strategies, hours: TradingHours,
  ai: AI, indicators: Indicators, notifications: Notifications,
  general: PLACEHOLDERS.general, users: PLACEHOLDERS.users, api: PLACEHOLDERS.api,
  compliance: PLACEHOLDERS.compliance, backups: PLACEHOLDERS.backups,
  billing: PLACEHOLDERS.billing, system: SystemHealth,
};

// Legacy view-id redirects (retired menu items -> a sensible landing leaf).
export const REDIRECTS = { configuration: "brokers" };

export const LEAVES = NAV.flatMap(g =>
  g.items.flatMap(it => (it.children ? it.children : [it])));

export function leafLabel(id) {
  const l = LEAVES.find(x => x.id === id);
  return l ? l.label : id;
}

// The Settings subgroup label containing `id`, or null for a top-level leaf.
export function parentTitleOf(id) {
  for (const g of NAV) {
    for (const it of g.items) {
      if (it.children && it.children.some(c => c.id === id)) return it.label;
    }
  }
  return null;
}

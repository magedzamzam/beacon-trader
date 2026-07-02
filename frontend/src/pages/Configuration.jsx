import { useState } from "react";
import {
  Building2, Rss, Coins, ShieldCheck, DollarSign, GitBranch, Clock,
  Sparkles, Bell, Settings, Users, KeyRound, FileCheck, Database,
  CreditCard, Activity, Plug,
} from "lucide-react";

import Brokers from "./Brokers";
import Sources from "./Sources";
import Symbols from "./Symbols";
import Risk from "./Risk";
import AI from "./AI";
import Currency from "./settings/Currency";
import Placeholder from "../components/Placeholder";

/**
 * Configuration — one home for everything that used to be its own menu item
 * (Brokers, Accounts, Sources, Symbols, Risk, AI) plus new enterprise-level
 * settings. Functional tabs render the existing feature components; every other
 * tab renders a documented Placeholder so the platform's full, stable shape is
 * visible today. All placeholders are catalogued in docs/CONFIGURATION.md.
 */
const GROUPS = [
  {
    title: "Connectivity",
    tabs: [
      { id: "brokers", label: "Brokers & Accounts", icon: Building2, render: () => <Brokers /> },
      { id: "sources", label: "Signal Sources", icon: Rss, render: () => <Sources /> },
      { id: "symbols", label: "Symbols", icon: Coins, render: () => <Symbols /> },
      {
        id: "integrations", label: "Integrations", icon: Plug, render: () => (
          <Placeholder icon={Plug} title="Integrations"
            description="Connect the platform to the rest of your stack — additional brokers, data vendors, and downstream tools."
            planned={[
              "Additional broker adapters (MetaTrader, IBKR, OANDA, Binance)",
              "Market-data vendor keys (Polygon, Twelve Data, Alpha Vantage)",
              "Outbound sync to Google Sheets / Notion / Airtable",
              "Zapier / Make webhook catalog",
              "Per-integration health and last-sync status",
            ]} />
        ),
      },
    ],
  },
  {
    title: "Trading",
    tabs: [
      { id: "risk", label: "Risk & Limits", icon: ShieldCheck, render: () => <Risk /> },
      { id: "currency", label: "Currency & FX", icon: DollarSign, render: () => <Currency /> },
      {
        id: "strategies", label: "Strategies", icon: GitBranch, render: () => (
          <Placeholder icon={GitBranch} title="Strategy Templates"
            description="Reusable execution strategies you can assign to sources instead of hand-tuning each one."
            planned={[
              "Named strategy templates (scalp, swing, DCA, grid)",
              "Partial-take-profit ladders and trailing-stop presets",
              "Break-even and stop-to-entry automation rules",
              "Per-source strategy assignment with overrides",
              "Backtest a strategy against historical signals",
            ]} />
        ),
      },
      {
        id: "hours", label: "Trading Hours", icon: Clock, render: () => (
          <Placeholder icon={Clock} title="Trading Hours & Sessions"
            description="Control when the bot is allowed to open positions and when it should stand down."
            planned={[
              "Per-market session windows (London / New York / Asia)",
              "News blackout windows (high-impact economic events)",
              "Weekend and holiday calendar",
              "Daily max-trades and cool-down between entries",
              "Timezone-aware scheduling",
            ]} />
        ),
      },
    ],
  },
  {
    title: "Intelligence",
    tabs: [
      { id: "ai", label: "AI Validation", icon: Sparkles, render: () => <AI /> },
      {
        id: "notifications", label: "Notifications", icon: Bell, render: () => (
          <Placeholder icon={Bell} title="Notifications & Alerts"
            description="Decide who gets told what, where, and how urgently when the bot acts."
            planned={[
              "Channels: email, Telegram, Slack, Discord, SMS, webhook",
              "Per-event routing (fill, SL hit, TP hit, error, AI reject)",
              "Severity thresholds and quiet hours",
              "Daily / weekly performance digest scheduling",
              "Escalation when a broker connection drops",
            ]} />
        ),
      },
    ],
  },
  {
    title: "Platform",
    tabs: [
      {
        id: "general", label: "General", icon: Settings, render: () => (
          <Placeholder icon={Settings} title="General Settings"
            description="Platform-wide identity and defaults."
            planned={[
              "Platform name, logo and brand color",
              "Default timezone and locale",
              "Default theme (dark / light / system)",
              "Number and date formatting",
              "Data retention windows",
            ]} />
        ),
      },
      {
        id: "users", label: "Users & Roles", icon: Users, render: () => (
          <Placeholder icon={Users} title="Users & Roles"
            description="Multi-user access with role-based permissions for a trading desk."
            planned={[
              "Invite team members by email",
              "Roles: Admin, Trader, Analyst, Read-only",
              "Two-factor authentication enforcement",
              "SSO / SAML for enterprise identity",
              "Per-account and per-broker access scoping",
              "Session timeout and device management",
            ]} />
        ),
      },
      {
        id: "api", label: "API & Webhooks", icon: KeyRound, render: () => (
          <Placeholder icon={KeyRound} title="API Keys & Webhooks"
            description="Programmatic access to the platform and inbound signal endpoints."
            planned={[
              "Scoped personal access tokens with expiry",
              "Inbound webhook endpoints per source with signing secrets",
              "Rate limits and IP allow-listing",
              "Key rotation and revocation with audit trail",
              "OpenAPI reference and request logs",
            ]} />
        ),
      },
      {
        id: "compliance", label: "Compliance & Audit", icon: FileCheck, render: () => (
          <Placeholder icon={FileCheck} title="Compliance & Audit"
            description="An immutable record of everything the platform and its users did."
            planned={[
              "Immutable audit log of config and trade actions",
              "Full trade blotter export (CSV / PDF)",
              "Regulatory / tax reports by period",
              "Data residency and retention controls",
              "Per-user activity trail",
            ]} />
        ),
      },
      {
        id: "backups", label: "Backups & Data", icon: Database, render: () => (
          <Placeholder icon={Database} title="Backups & Data"
            description="Protect configuration and history, and move it between environments."
            planned={[
              "Scheduled encrypted database backups",
              "One-click restore to a point in time",
              "Export / import full configuration as a bundle",
              "Encryption key (SECRET_KEY) management and rotation",
              "Retention policy per data class",
            ]} />
        ),
      },
      {
        id: "billing", label: "Billing & Usage", icon: CreditCard, render: () => (
          <Placeholder icon={CreditCard} title="Billing & Usage"
            description="Plan, metered usage, and cost controls."
            planned={[
              "Current subscription plan and limits",
              "Usage metering (API calls, AI tokens, active accounts)",
              "Invoices and payment methods",
              "Cost alerts and hard caps",
              "Per-team cost allocation",
            ]} />
        ),
      },
      {
        id: "system", label: "System Health", icon: Activity, render: () => (
          <Placeholder icon={Activity} title="System Health"
            description="Live operational status of the services behind the bot."
            planned={[
              "Service status: API, worker, database, broker links",
              "Queue depth and processing latency",
              "Build / version info and changelog",
              "Restart and maintenance-mode controls",
              "Live log tail and error rate",
            ]} />
        ),
      },
    ],
  },
];

const ALL_TABS = GROUPS.flatMap(g => g.tabs);

export default function Configuration({ initialTab }) {
  const [active, setActive] = useState(
    ALL_TABS.some(t => t.id === initialTab) ? initialTab : ALL_TABS[0].id
  );
  const current = ALL_TABS.find(t => t.id === active) || ALL_TABS[0];

  return (
    <div className="flex flex-col md:flex-row gap-4 md:gap-6">
      {/* Mobile: horizontally scrollable tab chips */}
      <nav className="md:hidden -mx-1 px-1 flex gap-2 overflow-x-auto pb-1">
        {ALL_TABS.map(({ id, label, icon: Icon }) => (
          <button key={id} onClick={() => setActive(id)}
            className={`shrink-0 flex items-center gap-2 px-3 py-2 rounded-lg text-sm whitespace-nowrap transition
              ${active === id ? "bg-beacon/10 text-beacon" : "bg-panel2 text-muted hover:text-ink"}`}>
            <Icon className="w-4 h-4" /> {label}
          </button>
        ))}
      </nav>

      {/* Desktop: grouped vertical tab list */}
      <nav className="hidden md:block w-56 shrink-0 space-y-5">
        {GROUPS.map(g => (
          <div key={g.title}>
            <div className="px-3 text-[10px] uppercase tracking-[0.16em] text-muted mb-1.5">{g.title}</div>
            {g.tabs.map(({ id, label, icon: Icon }) => (
              <button key={id} onClick={() => setActive(id)}
                className={`w-full flex items-center gap-3 px-3 py-2 rounded-lg text-sm mb-0.5 text-left transition
                  ${active === id ? "bg-beacon/10 text-beacon" : "text-muted hover:text-ink hover:bg-panel2"}`}>
                <Icon className="w-4 h-4 shrink-0" /> {label}
              </button>
            ))}
          </div>
        ))}
      </nav>

      <div className="flex-1 min-w-0">
        <div className="hidden md:flex items-center gap-2 mb-4">
          {current.icon && <current.icon className="w-4 h-4 text-beacon" />}
          <h2 className="text-sm font-medium">{current.label}</h2>
        </div>
        {current.render()}
      </div>
    </div>
  );
}

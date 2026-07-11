import {
  Settings, Users, KeyRound, FileCheck, Database, CreditCard, Plug, GitBranch,
} from "lucide-react";
import Placeholder from "../Placeholder";

/**
 * Routable placeholder views for the unbuilt Settings leaves (#57). Extracted
 * from the old Configuration page so each is a first-class view in the unified
 * sidebar. Catalogued in docs/CONFIGURATION.md.
 */
export const PLACEHOLDERS = {
  integrations: () => (
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
  strategies: () => (
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
  general: () => (
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
  users: () => (
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
  api: () => (
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
  compliance: () => (
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
  backups: () => (
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
  billing: () => (
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
};

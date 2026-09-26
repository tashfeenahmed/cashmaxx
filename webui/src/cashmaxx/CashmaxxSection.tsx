import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

import { SettingsSectionTitle } from "@/components/settings/shared/SettingsControls";
import { SegmentedControl } from "@/components/ui/segmented-control";
import { useClient } from "@/providers/ClientProvider";

import type { CashmaxxConnection } from "./api";
import { CashmaxxApprovals } from "./components/CashmaxxApprovals";
import { CashmaxxOverview } from "./components/CashmaxxOverview";
import { CashmaxxSettingsForm } from "./components/CashmaxxSettingsForm";
import { OwnerSessionProvider } from "./owner-session";

export type CashmaxxPage = "overview" | "approvals" | "settings";

/** Settings-page entry point: reads the WebUI token and socket from the app's ClientProvider. */
export function CashmaxxSettingsSection() {
  const { client, token } = useClient();
  const connection = useMemo<CashmaxxConnection>(() => ({ token, transport: client }), [client, token]);
  return <CashmaxxSection connection={connection} />;
}

/** The Cashmaxx section: wallet & P&L, approvals, and the guard's settings. */
export function CashmaxxSection({
  connection,
  initialPage = "overview",
}: {
  connection: CashmaxxConnection;
  initialPage?: CashmaxxPage;
}) {
  const { t } = useTranslation();
  const [page, setPage] = useState<CashmaxxPage>(initialPage);
  const pages: CashmaxxPage[] = ["overview", "approvals", "settings"];

  return (
    <OwnerSessionProvider connection={connection}>
      <section className="settings-stack" aria-labelledby="cashmaxx-section-title">
        <div className="settings-section-heading">
          <SettingsSectionTitle>
            <span id="cashmaxx-section-title">{t("cashmaxx.title")}</span>
          </SettingsSectionTitle>
          <SegmentedControl
            mode="tabs"
            value={page}
            ariaLabel={t("cashmaxx.pagesLabel")}
            options={pages.map((value) => ({ value, label: t(`cashmaxx.pages.${value}`) }))}
            onChange={setPage}
            className="max-w-full"
          />
        </div>
        <div role="tabpanel" aria-label={t(`cashmaxx.pages.${page}`)} key={page}>
          {page === "overview" ? <CashmaxxOverview connection={connection} /> : null}
          {page === "approvals" ? <CashmaxxApprovals connection={connection} /> : null}
          {page === "settings" ? <CashmaxxSettingsForm connection={connection} /> : null}
        </div>
      </section>
    </OwnerSessionProvider>
  );
}

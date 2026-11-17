import { lazy, Suspense } from "react";
import { Navigate, Route, Routes, useLocation } from "react-router";
import Alert from "@cloudscape-design/components/alert";
import Box from "@cloudscape-design/components/box";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Spinner from "@cloudscape-design/components/spinner";

import { useConfiguration } from "@/api/configuration";
import DashboardPage from "@/routes/dashboard";
import ReviewPage from "@/routes/review";
import TrackerPage from "@/routes/tracker";
import JobsPage from "@/routes/jobs";
import ApplyHealthPage from "@/routes/apply-health";
import { isMonitoringPath } from "@/components/shell/zones";
import { Shell } from "@/components/shell/Shell";
import SetupChecklistPage from "@/routes/setup-checklist";
import ConfigurationPage from "@/routes/configuration";
import SpamReviewPage from "@/routes/spam-review";
import RunsPage from "@/routes/runs";
import ScannerRunsPage from "@/routes/scanner-runs";
import ApplicationQueuePage from "@/routes/application-queue";
import SourcesPage from "@/routes/sources";
import TriagePage from "@/routes/triage";
import OnboardingResumePage from "@/routes/onboarding-resume";
import OnboardingSearchesPage from "@/routes/onboarding-searches";
import OnboardingCompaniesPage from "@/routes/onboarding-companies";
import OnboardingProviderPage from "@/routes/onboarding-provider";
import OnboardingReviewPage from "@/routes/onboarding-review";
import MyProfilePage from "@/routes/my-profile";

// Keep dashboard routes lazy so ordinary triage startup stays small.
const InsightsPage = lazy(() => import("@/routes/insights"));
const PerformancePage = lazy(() => import("@/routes/performance"));
const ApplierTracePage = lazy(() => import("@/routes/applier-trace"));

function InsightsFallback() {
  return <Loading label="Loading insights" />;
}

function PerformanceFallback() {
  return <Loading label="Loading performance" />;
}

export default function App() {
  const location = useLocation();
  const configuration = useConfiguration();

  const monitoring = isMonitoringPath(location.pathname, location.search);

  if (!monitoring && configuration.isPending) {
    return <ConfigurationLoading />;
  }
  if (!monitoring && (configuration.isError || configuration.data === undefined)) {
    return <ConfigurationUnavailable />;
  }
  if (!monitoring && !configuration.data?.configured && !location.pathname.startsWith("/setup") && location.pathname !== "/profile") {
    return <Navigate to="/setup" replace />;
  }

  return (
    <Routes>
      <Route path="/setup" element={<SetupChecklistPage />} />
      <Route path="/setup/checklist" element={<Navigate to="/setup" replace />} />
      <Route path="/setup/settings" element={<ConfigurationPage />} />
      <Route path="/setup/provider" element={<OnboardingProviderPage />} />
      <Route path="/setup/resume" element={<OnboardingResumePage />} />
      <Route path="/setup/searches" element={<OnboardingSearchesPage />} />
      <Route path="/setup/companies" element={<OnboardingCompaniesPage />} />
      <Route path="/setup/review" element={<OnboardingReviewPage />} />
      <Route element={<Shell />}>
        <Route path="/profile" element={<MyProfilePage />} />
        <Route path="/answer-bank" element={<Navigate to="/profile?section=answers" replace />} />
        <Route path="/" element={<Navigate to="/dashboard" replace />} />
        <Route path="/dashboard" element={<DashboardPage />} />
        <Route path="/review" element={<ReviewPage />} />
        <Route path="/tracker" element={<TrackerPage />} />
        <Route path="/jobs" element={<JobsPage />} />
        <Route path="/apply-health" element={<ApplyHealthPage />} />
        <Route
          path="/applier-trace"
          element={
            <Suspense fallback={<Loading label="Loading Applier trace" />}>
              <ApplierTracePage />
            </Suspense>
          }
        />
        <Route path="/triage" element={<TriagePage />} />
        <Route path="/pipeline" element={<TriagePage />} />
        <Route path="/spam-review" element={<SpamReviewPage />} />
        <Route path="/library" element={<Navigate to="/runs?tab=postings" replace />} />
        <Route
          path="/insights"
          element={
            <Suspense fallback={<InsightsFallback />}>
              <InsightsPage />
            </Suspense>
          }
        />
        <Route path="/runs" element={<RunsPage />} />
        <Route path="/scanner-runs" element={<ScannerRunsPage />} />
        <Route path="/application-queue" element={<ApplicationQueuePage />} />
        <Route
          path="/performance"
          element={
            <Suspense fallback={<PerformanceFallback />}>
              <PerformancePage />
            </Suspense>
          }
        />
        <Route path="/sources" element={<SourcesPage />} />
        <Route path="*" element={<Navigate to="/runs" replace />} />
      </Route>
    </Routes>
  );
}

function ConfigurationLoading() {
  return <Loading label="Loading settings" />;
}

function ConfigurationUnavailable() {
  return (
    <Box padding="xxl">
      <Alert type="error" header="Settings could not be loaded">
        Check the Jobfeed terminal for the error, then refresh this page.
      </Alert>
    </Box>
  );
}

function Loading({ label }: { label: string }) {
  return (
    <Box padding="xxl" textAlign="center">
      <SpaceBetween size="s" alignItems="center">
        <Spinner size="large" />
        <Box color="text-body-secondary">{label}</Box>
      </SpaceBetween>
    </Box>
  );
}

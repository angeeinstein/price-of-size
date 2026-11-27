import { Navigate, Route, Routes } from "react-router";
import { Layout } from "@/components/Layout";
import { ReauthDialog } from "@/components/ReauthDialog";
import { AuthProvider, useAuth } from "@/lib/auth";
import { AccountPage } from "@/pages/Account";
import { CategoriesPage } from "@/pages/Categories";
import { DomainsPage } from "@/pages/Domains";
import { ExplorerPage } from "@/pages/Explorer";
import { JobsPage } from "@/pages/Jobs";
import { LoginPage } from "@/pages/Login";
import { NodesPage } from "@/pages/Nodes";
import { OverviewPage } from "@/pages/Overview";
import { PricesPage } from "@/pages/Prices";
import { ReleasesPage } from "@/pages/Releases";
import { SettingsPage } from "@/pages/Settings";
import { SourcesPage } from "@/pages/Sources";
import { UsersPage } from "@/pages/Users";

function Gate() {
  const { me, loading } = useAuth();
  if (loading) return <div className="grid h-full place-items-center text-muted">Loading…</div>;
  if (!me) return <LoginPage />;
  return (
    <>
      <Layout>
        <Routes>
          <Route path="/" element={<OverviewPage />} />
          <Route path="/nodes" element={<NodesPage />} />
          <Route path="/jobs" element={<JobsPage />} />
          <Route path="/domains" element={<DomainsPage />} />
          <Route path="/sources" element={<SourcesPage />} />
          <Route path="/categories" element={<CategoriesPage />} />
          <Route path="/prices" element={<PricesPage />} />
          <Route path="/explorer" element={<ExplorerPage />} />
          <Route path="/releases" element={<ReleasesPage />} />
          <Route path="/settings" element={<SettingsPage />} />
          <Route path="/users" element={<UsersPage />} />
          <Route path="/account" element={<AccountPage />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </Layout>
      <ReauthDialog />
    </>
  );
}

export function App() {
  return (
    <AuthProvider>
      <Gate />
    </AuthProvider>
  );
}

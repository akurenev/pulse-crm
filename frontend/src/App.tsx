import { lazy, Suspense, type ReactNode } from "react";
import { Navigate, Outlet, Route, Routes } from "react-router-dom";

import { PwaInstallPrompt } from "./components/PwaInstallPrompt";
import { AppShell } from "./components/layout/AppShell";
import { DealsPage } from "./features/deals/DealsPage";
import { useAuth } from "./state/auth-store";

const DashboardPage = lazy(() => import("./pages/DashboardPage"));
const ContactsPage = lazy(() => import("./pages/ContactsPage"));
const TasksPage = lazy(() => import("./pages/TasksPage"));
const ActivityPage = lazy(() => import("./pages/ActivityPage"));
const SettingsPage = lazy(() => import("./pages/SettingsPage"));
const LoginPage = lazy(() => import("./pages/LoginPage"));
const AcceptInvitationPage = lazy(() => import("./pages/AcceptInvitationPage"));

const loadingFallback = <div className="route-loading" role="status">Загружаем рабочее пространство…</div>;

export default function App() {
  const { status } = useAuth();
  return (
    <>
      <Suspense fallback={loadingFallback}>
        <Routes>
          <Route path="/login" element={<LoginPage />} />
          <Route path="/accept-invitation" element={<AcceptInvitationPage />} />
          <Route element={<AuthGate />}>
            <Route element={<AppShell />}>
              <Route index element={<DashboardPage />} />
              <Route path="deals" element={<DealsPage />} />
              <Route path="contacts" element={<ContactsPage />} />
              <Route path="tasks" element={<TasksPage />} />
              <Route path="activity" element={<ActivityPage />} />
              <Route path="settings" element={<AdminOnly><SettingsPage /></AdminOnly>} />
            </Route>
          </Route>
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </Suspense>
      <PwaInstallPrompt enabled={status === "authenticated"} />
    </>
  );
}

function AuthGate() {
  const { status } = useAuth();
  if (status === "loading") return loadingFallback;
  if (status === "anonymous") return <Navigate to="/login" replace />;
  return <Outlet />;
}

function AdminOnly({ children }: { children: ReactNode }) {
  const { session } = useAuth();
  if (session?.user.role === "manager") return <Navigate to="/deals" replace />;
  return children;
}

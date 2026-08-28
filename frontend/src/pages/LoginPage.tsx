import { ArrowRight, LockKeyhole, Mail } from "lucide-react";
import { type FormEvent, useState } from "react";
import { useNavigate } from "react-router-dom";

import { BrandMark } from "../components/BrandMark";
import { Button } from "../components/Button";
import { ApiError, remoteEnabled } from "../lib/api";
import { useAuth } from "../state/auth-store";

export default function LoginPage() {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const navigate = useNavigate();
  const { login } = useAuth();

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    setLoading(true);
    setError("");
    try {
      await login(String(data.get("email") ?? ""), String(data.get("password") ?? ""));
      navigate("/deals", { replace: true });
    } catch (reason) {
      setError(reason instanceof ApiError && reason.status === 401
        ? "Неверный email или пароль"
        : "Не удалось войти. Попробуйте ещё раз.");
    } finally {
      setLoading(false);
    }
  }

  return (
    <main className="login-page">
      <section className="login-panel">
        <BrandMark />
        <div><h1>Войдите в Pulse CRM</h1><p>Сделки, клиенты и повторные покупки в одном рабочем пространстве.</p></div>
        <form onSubmit={(event) => void handleSubmit(event)} className="form-stack">
          <label className="field field--icon"><span>Email</span><span><Mail size={18} /><input name="email" type="email" required defaultValue={remoteEnabled ? "" : "owner@pulse.local"} autoComplete="email" /></span></label>
          <label className="field field--icon"><span>Пароль</span><span><LockKeyhole size={18} /><input name="password" type="password" required defaultValue={remoteEnabled ? "" : "pulse-demo"} autoComplete="current-password" /></span></label>
          {error ? <p className="form-error" role="alert">{error}</p> : null}
          <Button variant="primary" type="submit" disabled={loading}>{loading ? "Входим…" : <>Войти <ArrowRight size={17} /></>}</Button>
        </form>
        <small>Доступ выдаёт администратор компании</small>
      </section>
      <aside className="login-art" aria-hidden="true"><div><i /><i /><i /><i /></div><p>От первого обращения<br />до следующей покупки.</p></aside>
    </main>
  );
}

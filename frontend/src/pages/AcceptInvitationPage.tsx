import { ArrowRight, LockKeyhole, UserRound } from "lucide-react";
import { useState, type FormEvent } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";

import { BrandMark } from "../components/BrandMark";
import { Button } from "../components/Button";
import { useAuth } from "../state/auth-store";

export default function AcceptInvitationPage() {
  const [params] = useSearchParams();
  const token = params.get("token") ?? "";
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const { acceptInvitation } = useAuth();
  const navigate = useNavigate();

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    setSaving(true);
    setError("");
    try {
      await acceptInvitation(token, String(data.get("full_name")), String(data.get("password")));
      navigate("/deals", { replace: true });
    } catch {
      setError("Приглашение недействительно или уже использовано");
    } finally {
      setSaving(false);
    }
  }

  return (
    <main className="login-page">
      <section className="login-panel">
        <BrandMark />
        <div><h1>Присоединиться к Pulse CRM</h1><p>Укажите имя и задайте пароль для рабочего аккаунта.</p></div>
        {!token ? <p className="form-error" role="alert">В ссылке отсутствует token приглашения.</p> : (
          <form className="form-stack" onSubmit={(event) => void submit(event)}>
            <label className="field field--icon"><span>Имя</span><span><UserRound size={18} /><input name="full_name" required minLength={2} autoComplete="name" /></span></label>
            <label className="field field--icon"><span>Пароль</span><span><LockKeyhole size={18} /><input name="password" type="password" required minLength={12} autoComplete="new-password" /></span></label>
            {error ? <p className="form-error" role="alert">{error}</p> : null}
            <Button variant="primary" type="submit" disabled={saving}>{saving ? "Создаём аккаунт…" : <>Принять приглашение <ArrowRight size={17} /></>}</Button>
          </form>
        )}
        <small>Пароль должен содержать не менее 12 символов</small>
      </section>
      <aside className="login-art" aria-hidden="true"><div><i /><i /><i /><i /></div><p>Одна команда.<br />Один контекст продаж.</p></aside>
    </main>
  );
}

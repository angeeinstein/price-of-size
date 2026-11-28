import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router";
import { PageTitle } from "@/components/Layout";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardHeader } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Table, Td, Th, Tr } from "@/components/ui/table";
import { api, ApiError } from "@/lib/api";
import { useAuth, useIsAdmin } from "@/lib/auth";
import { cn } from "@/lib/utils";

// The form is built from /api/admin/settings/schema; the server checks every value.

type Kind = "bool" | "int" | "number" | "percent" | "enum" | "string" | "models" | "generation" | "managed";

interface Field {
  key: string;
  label: string;
  help: string;
  kind: Kind;
  min?: number;
  max?: number;
  step?: number;
  unit?: string;
  nullable?: boolean;
  null_label?: string;
  options?: { value: string; label: string }[];
  min_length?: number;
  max_length?: number;
  roles?: { role: string; label: string }[];
}

interface Model { id: string; label: string; input_usd: number; output_usd: number }

interface Schema {
  groups: { title: string; subtitle: string; fields: Field[] }[];
  defaults: Record<string, unknown>;
  models: Model[];
  unused: Record<string, string>;
}

type Values = Record<string, unknown>;

const same = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b);
const selectClass = "h-9 rounded-md border border-line bg-panel px-2 text-sm disabled:opacity-50";

function useSave(key: string) {
  const qc = useQueryClient();
  const { sensitive } = useAuth();
  return useMutation({
    mutationFn: (value: unknown) =>
      sensitive(() => api.put(`/api/admin/settings/${encodeURIComponent(key)}`, { value: value as never })),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["settings"] }),
  });
}

function errorText(e: unknown): string {
  return e instanceof ApiError || e instanceof Error ? e.message : String(e);
}

/** Shown value -> stored value, or an error message. */
function parse(field: Field, text: string): { value?: unknown; error?: string } {
  if (field.kind === "enum") return { value: text };
  if (field.kind === "string") {
    if (field.min_length != null && text.length < field.min_length) return { error: `at least ${field.min_length} characters` };
    if (field.max_length != null && text.length > field.max_length) return { error: `at most ${field.max_length} characters` };
    return { value: text };
  }
  if (text.trim() === "") return { error: "enter a number" };
  const n = Number(text);
  if (!Number.isFinite(n)) return { error: "enter a number" };
  if (field.kind === "percent") {
    if (n < 0 || n > 100) return { error: "between 0 and 100 %" };
    return { value: Number((n / 100).toFixed(6)) };
  }
  if (field.kind === "int" && !Number.isInteger(n)) return { error: "a whole number" };
  if (field.min != null && n < field.min) return { error: `at least ${field.min}` };
  if (field.max != null && n > field.max) return { error: `at most ${field.max}` };
  return { value: n };
}

function shown(field: Field, value: unknown): string {
  if (value == null) return "";
  if (field.kind === "percent") return String(Math.round(Number(value) * 10000) / 100);
  return String(value);
}

function describe(field: Field, value: unknown): string {
  if (value == null) return field.null_label ?? "none";
  if (field.kind === "bool") return value ? "on" : "off";
  if (field.kind === "percent") return `${shown(field, value)} %`;
  if (field.kind === "enum") return field.options?.find((o) => o.value === value)?.label ?? String(value);
  if (field.kind === "models") return "the default models";
  return field.unit ? `${String(value)} ${field.unit}` : String(value);
}

function Switch({ checked, onChange, disabled, label }: { checked: boolean; onChange: (v: boolean) => void; disabled?: boolean; label: string }) {
  return (
    <button type="button" role="switch" aria-checked={checked} aria-label={label} disabled={disabled}
      onClick={() => onChange(!checked)}
      className={cn("relative inline-flex h-6 w-11 shrink-0 items-center rounded-full transition-colors disabled:opacity-50",
        checked ? "bg-accent" : "bg-line")}>
      <span className={cn("inline-block h-5 w-5 rounded-full bg-white shadow transition-transform",
        checked ? "translate-x-5" : "translate-x-0.5")} />
    </button>
  );
}

/** A field whose edits are drafted and saved with a button. */
function DraftField({ field, value, fallback, models: choices }: { field: Field; value: unknown; fallback: unknown; models: Model[] }) {
  const admin = useIsAdmin();
  const save = useSave(field.key);
  const [text, setText] = useState(shown(field, value ?? (field.nullable ? fallback : null)));
  const [isNull, setIsNull] = useState(value == null && !!field.nullable);
  const [models, setModels] = useState<Record<string, string>>(
    field.kind === "models" ? { ...(value as Record<string, string>) } : {},
  );

  let draft: { value?: unknown; error?: string };
  if (field.kind === "models") draft = { value: models };
  else if (isNull) draft = { value: null };
  else draft = parse(field, text);
  const changed = draft.error === undefined && !same(draft.value, value);
  const reset = () => {
    setText(shown(field, value ?? (field.nullable ? fallback : null)));
    setIsNull(value == null && !!field.nullable);
    if (field.kind === "models") setModels({ ...(value as Record<string, string>) });
    save.reset();
  };

  let control;
  if (field.kind === "models") {
    control = (
      <div className="grid gap-2 sm:grid-cols-[12rem_1fr]">
        {field.roles?.map((r) => (
          <label key={r.role} className="contents">
            <span className="self-center text-sm">{r.label}</span>
            <select className={selectClass} disabled={!admin} aria-label={r.label}
              value={models[r.role] ?? models.default ?? ""}
              onChange={(e) => setModels({ ...models, [r.role]: e.target.value })}>
              {(models[r.role] ?? models.default) && !choices.some((m) => m.id === (models[r.role] ?? models.default)) && (
                <option value={models[r.role] ?? models.default}>{models[r.role] ?? models.default}</option>
              )}
              {choices.map((m) => (
                <option key={m.id} value={m.id}>{m.label} · ${m.input_usd.toFixed(2)} in / ${m.output_usd.toFixed(2)} out per M tokens</option>
              ))}
            </select>
          </label>
        ))}
      </div>
    );
  } else if (field.kind === "enum") {
    control = (
      <select className={selectClass} disabled={!admin} aria-label={field.label} value={text}
        onChange={(e) => setText(e.target.value)}>
        {field.options?.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
      </select>
    );
  } else {
    const numeric = field.kind !== "string";
    control = (
      <div className="flex flex-wrap items-center gap-2">
        <div className="flex items-center gap-2">
          <Input className={cn("h-9", numeric ? "w-32 text-right tabular-nums" : "w-full min-w-72")}
            type={numeric ? "number" : "text"} aria-label={field.label}
            min={field.kind === "percent" ? 0 : field.min} max={field.kind === "percent" ? 100 : field.max}
            step={field.kind === "percent" ? 0.01 : field.kind === "int" ? 1 : field.step ?? "any"}
            placeholder={isNull ? "" : field.min != null ? String(field.min) : ""}
            value={isNull ? "" : text} disabled={!admin || isNull} onChange={(e) => setText(e.target.value)} />
          {(field.kind === "percent" || field.unit) && (
            <span className="text-sm text-muted">{field.kind === "percent" ? "%" : field.unit}</span>
          )}
        </div>
        {field.nullable && (
          <label className="flex items-center gap-2 text-sm text-muted">
            <input type="checkbox" checked={isNull} disabled={!admin} onChange={(e) => setIsNull(e.target.checked)} />
            {field.null_label}
          </label>
        )}
      </div>
    );
  }

  return (
    <div className="space-y-1">
      {control}
      {draft.error && text !== "" && <div className="text-xs text-bad">{draft.error}</div>}
      {save.error && <div className="text-xs text-bad">{errorText(save.error)}</div>}
      {admin && changed && (
        <div className="flex gap-2 pt-1">
          <Button size="sm" disabled={save.isPending} onClick={() => save.mutate(draft.value)}>Save</Button>
          <Button size="sm" variant="ghost" onClick={reset}>Cancel</Button>
        </div>
      )}
    </div>
  );
}

function BoolField({ field, value }: { field: Field; value: unknown }) {
  const admin = useIsAdmin();
  const save = useSave(field.key);
  return (
    <div className="space-y-1">
      <Switch checked={!!value} disabled={!admin || save.isPending} label={field.label} onChange={(v) => save.mutate(v)} />
      {save.error && <div className="text-xs text-bad">{errorText(save.error)}</div>}
    </div>
  );
}

function GenerationField({ field, value }: { field: Field; value: unknown }) {
  const admin = useIsAdmin();
  const save = useSave(field.key);
  const n = Number(value ?? 0);
  return (
    <div className="space-y-1">
      <div className="flex items-center gap-3">
        <span className="text-sm tabular-nums">run {n}</span>
        {admin && (
          <Button size="sm" variant="outline" disabled={save.isPending}
            onClick={() => { if (window.confirm("Run every classifier again over all products?")) save.mutate(n + 1); }}>
            Run again
          </Button>
        )}
      </div>
      {save.error && <div className="text-xs text-bad">{errorText(save.error)}</div>}
    </div>
  );
}

function FieldRow({ field, values, defaults, models }: { field: Field; values: Values; defaults: Values; models: Model[] }) {
  const value = values[field.key];
  const fallback = defaults[field.key];
  const admin = useIsAdmin();
  const save = useSave(field.key);
  const isDefault = same(value, fallback);
  let control;
  if (field.kind === "bool") control = <BoolField field={field} value={value} />;
  else if (field.kind === "generation") control = <GenerationField field={field} value={value} />;
  else if (field.kind === "managed")
    control = <span className="text-sm">{value == null ? "stays on the current release" : String(value)} · <Link className="text-accent hover:underline" to="/releases">Releases</Link></span>;
  else control = <DraftField key={JSON.stringify(value)} field={field} value={value} fallback={fallback} models={models} />;
  return (
    <div className="grid gap-3 border-t border-line px-5 py-4 first:border-t-0 md:grid-cols-[minmax(0,2fr)_minmax(0,3fr)]">
      <div>
        <div className="text-sm font-medium">{field.label}</div>
        {field.help && <div className="mt-0.5 text-xs text-muted">{field.help}</div>}
        {!isDefault && field.kind !== "managed" && field.kind !== "generation" && (
          <div className="mt-1 flex items-center gap-2 text-xs text-muted">
            <span>default: {describe(field, fallback)}</span>
            {admin && (
              <button type="button" className="text-accent hover:underline disabled:opacity-50" disabled={save.isPending}
                onClick={() => save.mutate(fallback)}>reset</button>
            )}
          </div>
        )}
      </div>
      <div>{control}</div>
    </div>
  );
}

function JsonRow({ k, value, note }: { k: string; value: unknown; note?: string }) {
  const admin = useIsAdmin();
  const save = useSave(k);
  const [text, setText] = useState(JSON.stringify(value));
  const [error, setError] = useState<string | null>(null);
  const submit = () => {
    let parsed: unknown;
    try { parsed = JSON.parse(text); } catch { setError('enter JSON, e.g. 0.5, true, "text", null'); return; }
    setError(null);
    save.mutate(parsed);
  };
  const changed = text !== JSON.stringify(value);
  return (
    <Tr>
      <Td className="w-80">
        <div className="font-mono text-xs">{k}</div>
        {note && <div className="mt-1"><Badge>{note}</Badge></div>}
      </Td>
      <Td>
        <div className="flex items-center gap-2">
          <Input className="h-8 font-mono text-xs" value={text} disabled={!admin} onChange={(e) => setText(e.target.value)} aria-label={k} />
          {admin && changed && <Button size="sm" disabled={save.isPending} onClick={submit}>Save</Button>}
        </div>
        {(error || save.error) && <div className="mt-1 text-xs text-bad">{error ?? errorText(save.error)}</div>}
      </Td>
    </Tr>
  );
}

export function SettingsPage() {
  const settings = useQuery({ queryKey: ["settings"], queryFn: () => api.get<Values>("/api/admin/settings") });
  const schema = useQuery({ queryKey: ["settings-schema"], queryFn: () => api.get<Schema>("/api/admin/settings/schema"), staleTime: Infinity });
  const [json, setJson] = useState(false);
  const values = settings.data ?? {};
  return (
    <>
      <PageTitle title="Settings" subtitle="Runtime switches; changes apply within seconds and are audit-logged" />
      <div className="mb-4 flex justify-end">
        <label className="flex items-center gap-2 text-sm text-muted">
          <Switch checked={json} onChange={setJson} label="Edit as JSON" /> Edit as JSON
        </label>
      </div>
      {json ? (
        <Card>
          <CardHeader title="All settings as JSON" subtitle="Every key the coordinator knows, including ones not in the form" />
          <Table>
            <thead><tr><Th>Key</Th><Th>Value (JSON)</Th></tr></thead>
            <tbody>
              {Object.entries(values).map(([k, v]) => (
                <JsonRow key={`${k}:${JSON.stringify(v)}`} k={k} value={v} note={schema.data?.unused[k]} />
              ))}
            </tbody>
          </Table>
        </Card>
      ) : (
        <div className="space-y-6">
          {schema.data?.groups.map((g) => (
            <Card key={g.title}>
              <CardHeader title={g.title} subtitle={g.subtitle} />
              <div>
                {settings.data && g.fields.map((f) => (
                  <FieldRow key={f.key} field={f} values={values} defaults={schema.data.defaults} models={schema.data.models} />
                ))}
              </div>
            </Card>
          ))}
        </div>
      )}
    </>
  );
}

// The owner's name and picture: asked on the first step of the setup wizard and editable in
// Settings. Only the pages show them; no role's prompt includes either.
import { useEffect, useRef, useState } from "react";
import { api, REPLAY } from "./api";
import { owner, ownerAvatar, OwnerAv } from "./ui";

const MAX_PHOTO = 5 * 1024 * 1024;

/** An unsaved name and picture. `save` writes only what changed. */
export function useProfileDraft(named: boolean) {
  const [name, setName] = useState(named ? owner() : "");
  const [photo, setPhoto] = useState<File | null>(null);
  const [drop, setDrop] = useState(false); // go back to initials
  const [preview, setPreview] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  useEffect(() => {
    if (!photo) { setPreview(null); return; }
    const url = URL.createObjectURL(photo);
    setPreview(url);
    return () => URL.revokeObjectURL(url);
  }, [photo]);
  const saved = ownerAvatar();
  const picture = photo ? preview : drop ? null : saved;
  const clean = name.trim().replace(/\s+/g, " ");
  const dirty = (!!clean && (!named || clean !== owner())) || !!photo || (drop && !!saved);
  const pick = (file: File | undefined) => {
    setError(null);
    if (!file) return;
    if (!["image/png", "image/jpeg"].includes(file.type)) { setError("Use a PNG or JPEG photo"); return; }
    if (file.size > MAX_PHOTO) { setError("Use a photo of up to 5 MB"); return; }
    setPhoto(file);
    setDrop(false);
  };
  const clear = () => { setPhoto(null); setDrop(true); setError(null); };
  const save = async (): Promise<boolean> => {
    setError(null);
    setSaving(true);
    try {
      if (clean && (!named || clean !== owner())) await api.setOwner(clean);
      if (photo) await api.ownerPhoto(photo);
      else if (drop && saved) await api.clearOwnerPhoto();
      setPhoto(null);
      setDrop(false);
      return true;
    } catch (e) {
      setError((e as Error).message);
      return false;
    } finally {
      setSaving(false);
    }
  };
  return { name, setName, clean, picture, pick, clear, save, dirty, error, saving };
}

export type ProfileDraft = ReturnType<typeof useProfileDraft>;

/** The name field, the picture (or initials) and the photo buttons. */
export function ProfileFields({ d, autoFocus }: { d: ProfileDraft; autoFocus?: boolean }) {
  const file = useRef<HTMLInputElement>(null);
  return (
    <div className="you-card">
      <OwnerAv size="lg" name={d.clean || "?"} picture={d.picture} />
      <div className="col" style={{ gap: 8, minWidth: 0, flex: 1 }}>
        <label className="you-l" htmlFor="owner-name">Your name</label>
        <input id="owner-name" className="you-in" value={d.name} maxLength={40} autoFocus={autoFocus}
               placeholder="How the team should address you" disabled={REPLAY}
               onChange={(e) => d.setName(e.target.value)} />
        <div className="muted" style={{ fontSize: 12 }}>
          The team mentions you by this name in the Team Room. Without a photo, your initials stand in.</div>
        <div className="row" style={{ gap: 8 }}>
          <input ref={file} type="file" accept="image/png,image/jpeg" hidden
                 onChange={(e) => { d.pick(e.target.files?.[0]); e.target.value = ""; }} />
          <button className="btn ghost xs" type="button" disabled={REPLAY}
                  onClick={() => file.current?.click()}>{d.picture ? "Change photo" : "Add a photo"}</button>
          {d.picture && <button className="btn ghost xs" type="button" disabled={REPLAY}
                                onClick={d.clear}>Use initials</button>}
          <span className="mono muted" style={{ fontSize: 11 }}>PNG or JPEG, up to 5 MB, cropped square</span>
        </div>
        {d.error && <div className="err" style={{ fontSize: 12 }}>{d.error}</div>}
      </div>
    </div>
  );
}

/** Settings: the same fields, saved with a button. */
export function ProfileForm({ named }: { named: boolean }) {
  const d = useProfileDraft(named);
  const [done, setDone] = useState(false);
  return (
    <section className="section" style={{ maxWidth: 640 }}>
      <div className="h2">Your profile</div>
      <ProfileFields d={d} />
      <div className="row" style={{ gap: 8 }}>
        <button className="btn pri" disabled={REPLAY || !d.clean || !d.dirty || d.saving}
                onClick={() => { setDone(false); d.save().then(setDone); }}>Save</button>
        {done && !d.dirty && <span className="muted" style={{ fontSize: 12 }}>Saved</span>}
        {REPLAY && <span className="muted" style={{ fontSize: 12 }}>A replay is read-only.</span>}
      </div>
    </section>
  );
}

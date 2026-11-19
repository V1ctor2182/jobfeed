/** The sections of My Profile, in sidebar order. The `section` URL parameter selects one. */
export const PROFILE_SECTIONS = [
  { id: "overview", title: "Overview", subtitle: "Profile strength and résumé" },
  { id: "details", title: "Profile", subtitle: "Edit autofill information" },
  { id: "personal", title: "Personal info", subtitle: "Edit demographic data" },
  { id: "answers", title: "Application answers", subtitle: "Questions employers ask" },
  { id: "resume", title: "Résumé", subtitle: "Upload and choose your résumé" },
  { id: "preferences", title: "Job preferences", subtitle: "Refine your job search" },
  { id: "login", title: "Login", subtitle: "Password for employer accounts" },
] as const;

export type ProfileSection = (typeof PROFILE_SECTIONS)[number]["id"];

export function profileSection(value: string | null): ProfileSection {
  return PROFILE_SECTIONS.find((section) => section.id === value)?.id ?? "overview";
}

export function sectionHref(section: ProfileSection): string {
  return `/profile?section=${section}`;
}

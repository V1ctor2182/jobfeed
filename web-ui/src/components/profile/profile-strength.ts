import type { CommonQuestion } from "@/api/answer-bank";
import type { PersonalDetails, ResumeEntry } from "@/api/my-profile";
import type { ProfileSection } from "./sections";

export interface StrengthInput {
  personal?: PersonalDetails;
  resumes?: ResumeEntry[];
  common?: CommonQuestion[];
  preferencesConfirmed: boolean;
}

const filled = (...values: (string | undefined)[]) => values.every((value) => Boolean(value?.trim()));
const answered = (common: CommonQuestion[] | undefined, group: CommonQuestion["group"]) => {
  const items = common?.filter((item) => item.group === group) ?? [];
  return items.length > 0 && items.every((item) => item.entry_id);
};

/** What a complete profile has, each with its share of the strength score. The weights sum to 100. */
export const STRENGTH_ITEMS: { label: string; weight: number; section: ProfileSection; done: (input: StrengthInput) => boolean }[] = [
  { label: "Add your contact info", weight: 15, section: "details",
    done: ({ personal: p }) => filled(p?.legal_name || p?.preferred_name, p?.primary_email, p?.phone) },
  { label: "Add your LinkedIn, GitHub or portfolio", weight: 10, section: "details",
    done: ({ personal: p }) => filled(p?.linkedin_url) || filled(p?.github_url) || filled(p?.portfolio_url) },
  { label: "Add work authorization and sponsorship", weight: 15, section: "details",
    done: ({ personal: p }) => filled(p?.work_authorization, p?.sponsorship_requirement) },
  { label: "Add your education", weight: 10, section: "details", done: ({ personal: p }) => filled(p?.education) },
  { label: "Add a résumé for applications", weight: 15, section: "resume", done: ({ resumes }) => Boolean(resumes?.some((resume) => resume.active)) },
  { label: "Answer the demographic questions", weight: 15, section: "personal", done: ({ common }) => answered(common, "demographic") },
  { label: "Answer the common application questions", weight: 10, section: "answers", done: ({ common }) => answered(common, "application") },
  { label: "Save your job preferences", weight: 10, section: "preferences", done: ({ preferencesConfirmed }) => preferencesConfirmed },
];

export function profileStrength(input: StrengthInput) {
  const items = STRENGTH_ITEMS.map((item) => ({ ...item, complete: item.done(input) }));
  return { percent: items.reduce((sum, item) => sum + (item.complete ? item.weight : 0), 0), items };
}

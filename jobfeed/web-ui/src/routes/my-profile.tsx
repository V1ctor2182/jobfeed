import type { ReactNode } from "react";
import Alert from "@cloudscape-design/components/alert";
import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import Container from "@cloudscape-design/components/container";
import ContentLayout from "@cloudscape-design/components/content-layout";
import Grid from "@cloudscape-design/components/grid";
import Header from "@cloudscape-design/components/header";
import SpaceBetween from "@cloudscape-design/components/space-between";
import { useNavigate, useSearchParams } from "react-router";
import { useAnswerBank } from "@/api/answer-bank";
import { useResumeOnboarding } from "@/api/onboarding-resume";
import { useMyProfile } from "@/api/my-profile";
import { useProfilePreferences } from "@/api/my-profile-preferences";
import { PersonalDetailsEditor } from "@/components/profile/PersonalDetailsEditor";
import { ResumeEditor } from "@/components/profile/ResumeEditor";
import { JobPreferencesEditor } from "@/components/profile/JobPreferencesEditor";
import { LoginSection } from "@/components/profile/LoginSection";
import { ApplicationAnswersSection, PersonalInfoSection } from "@/components/profile/ProfileAnswers";
import { ProfileOverview } from "@/components/profile/ProfileOverview";
import { PROFILE_SECTIONS, type ProfileSection, profileSection, sectionHref } from "@/components/profile/sections";
import "./my-profile.css";

export default function MyProfilePage() {
  const personal = useMyProfile();
  const preferences = useProfilePreferences();
  const resume = useResumeOnboarding();
  const answers = useAnswerBank();
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const active = profileSection(params.get("section"));
  const details = personal.data?.personal_details;
  const activeResume = personal.data?.resumes.find((entry) => entry.active && entry.kind !== "master");
  const name = details?.preferred_name || details?.legal_name || "Your profile";

  // Sections with editors stay mounted so unsaved edits survive switching sections.
  const sections: Record<ProfileSection, ReactNode> = {
    overview: active === "overview" && <ProfileOverview activeResume={activeResume}
      input={{ personal: details, resumes: personal.data?.resumes, common: answers.data?.common_questions, preferencesConfirmed: resume.data?.is_confirmed ?? false }} />,
    details: personal.data && <PersonalDetailsEditor initial={personal.data} />,
    personal: <PersonalInfoSection />,
    answers: <ApplicationAnswersSection />,
    resume: personal.data && <ResumeEditor resumes={personal.data.resumes} />,
    preferences: <>
      {preferences.isPending && <Box><div role="status">Loading job preferences.</div></Box>}
      {preferences.isError && <Alert type="error" header="Job preferences could not be loaded" action={<Button onClick={() => { void preferences.refetch(); }}>Retry job preferences</Button>}>{preferences.error.message}</Alert>}
      {preferences.data && <JobPreferencesEditor state={preferences.data} confirmed={resume.data?.is_confirmed ?? false} />}
    </>,
    login: active === "login" && <LoginSection />,
  };

  return <ContentLayout maxContentWidth={1280} header={<Header variant="h1"
    description="Everything Appliers fill in for you. Save each section when you finish reviewing it. Saving does not start job searches or applications."
    actions={<Button onClick={() => navigate("/setup")}>Back to setup checklist</Button>}>My Profile</Header>}>
    <Grid gridDefinition={[{ colspan: { default: 12, s: 4 } }, { colspan: { default: 12, s: 8 } }]}>
      <div className="profile-aside"><Container>
        <SpaceBetween size="m">
          <div>
            <Box variant="h2" tagOverride="p">{name}</Box>
            {activeResume?.role && <Box color="text-body-secondary">{activeResume.role}</Box>}
          </div>
          <nav aria-label="Profile sections">
            <ul className="profile-nav">
              {PROFILE_SECTIONS.map((section) => <li key={section.id}>
                <a href={sectionHref(section.id)} aria-current={section.id === active ? "page" : undefined}
                  onClick={(event) => { event.preventDefault(); setParams({ section: section.id }); }}>
                  <span className="profile-nav-title">{section.title}</span>
                  <span className="profile-nav-subtitle">{section.subtitle}</span>
                </a>
              </li>)}
            </ul>
          </nav>
        </SpaceBetween>
      </Container></div>
      <SpaceBetween size="l">
        {personal.isPending && <Box><div role="status">Loading your personal details.</div></Box>}
        {personal.isError && <Alert type="error" header="Personal details could not be loaded" action={<Button onClick={() => { void personal.refetch(); }}>Retry personal details</Button>}>{personal.error.message}</Alert>}
        {PROFILE_SECTIONS.map((section) => <div key={section.id} hidden={section.id !== active}>
          <SpaceBetween size="l">{sections[section.id]}</SpaceBetween>
        </div>)}
      </SpaceBetween>
    </Grid>
  </ContentLayout>;
}

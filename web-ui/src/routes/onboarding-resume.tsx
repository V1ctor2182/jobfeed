import Alert from "@cloudscape-design/components/alert";
import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import Container from "@cloudscape-design/components/container";
import ContentLayout from "@cloudscape-design/components/content-layout";
import Header from "@cloudscape-design/components/header";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Spinner from "@cloudscape-design/components/spinner";
import Textarea from "@cloudscape-design/components/textarea";
import { useNavigate } from "react-router";
import { useAnalyzeResume, useResumeOnboarding } from "@/api/onboarding-resume";

export default function OnboardingResumePage() {
  const navigate = useNavigate();
  const resume = useResumeOnboarding();
  const analyze = useAnalyzeResume();

  if (resume.isPending) return <Box padding="xxl" textAlign="center"><Spinner size="large" /></Box>;
  if (resume.isError || resume.data === undefined) {
    return <Box padding="xxl"><Alert type="error">Résumé setup could not be loaded.</Alert></Box>;
  }
  const state = resume.data;
  return <ContentLayout maxContentWidth={1080} header={<Header variant="h1"
    description="Upload your résumé and save job preferences in My Profile. AI analysis is optional."
    actions={<Button onClick={() => navigate("/setup")}>Back to setup</Button>}>Résumé and job preferences</Header>}>
    <SpaceBetween size="l">
      <Container header={<Header variant="h2">Manage your profile</Header>}>
        <SpaceBetween size="m">
          <Box>My Profile is the place to upload résumés, edit your preferences, and save them for job discovery.</Box>
          {state.original_name && <Box>Current résumé: {state.original_name}</Box>}
          <SpaceBetween size="s" direction="horizontal">
            <Button variant="primary" onClick={() => navigate("/profile?section=resume")}>Open My Profile</Button>
            {state.is_confirmed && <Button onClick={() => navigate("/setup/searches")}>Continue to review searches</Button>}
          </SpaceBetween>
        </SpaceBetween>
      </Container>
      {state.extracted_text && <Container header={<Header variant="h2" description="Optional suggestions from your selected AI provider.">Résumé analysis</Header>}>
        <SpaceBetween size="m">
          <Textarea ariaLabel="Extracted résumé text" value={state.extracted_text} readOnly rows={12} />
          <Alert type="info">Analysis sends this extracted text to your chosen AI provider and creates a preference draft. Review and save the draft in My Profile before using it.</Alert>
          <Button loading={analyze.isPending} onClick={() => analyze.mutate(undefined)}>Analyze résumé</Button>
          {analyze.isSuccess && <Alert type="success" action={<Button onClick={() => navigate("/profile?section=preferences")}>Review suggestions in My Profile</Button>}>Suggestions are ready to review.</Alert>}
          {analyze.error && <Alert type="error">{analyze.error.message}</Alert>}
        </SpaceBetween>
      </Container>}
    </SpaceBetween>
  </ContentLayout>;
}

import { useState } from "react";
import Alert from "@cloudscape-design/components/alert";
import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import Container from "@cloudscape-design/components/container";
import FormField from "@cloudscape-design/components/form-field";
import Header from "@cloudscape-design/components/header";
import Input from "@cloudscape-design/components/input";
import SpaceBetween from "@cloudscape-design/components/space-between";
import StatusIndicator from "@cloudscape-design/components/status-indicator";

import { useAccountPassword, useSaveAccountPassword } from "@/api/account-password";

/** The one password used wherever an employer site needs an account. It can be replaced here and is never shown. */
export function LoginSection() {
  const status = useAccountPassword();
  const save = useSaveAccountPassword();
  const [password, setPassword] = useState("");
  const accounts = status.data?.accounts ?? 0;

  return (
    <Container header={<Header variant="h2" description="One password for every employer site that needs an account. A program types it, not an Applier, and it is never shown again.">Login</Header>}>
      <SpaceBetween size="m">
        {status.isError && <Alert type="error">The password status could not be loaded.</Alert>}
        {status.data && (
          <StatusIndicator type={status.data.password_set ? "success" : "warning"}>
            {status.data.password_set ? `A password is set. ${accounts} ${accounts === 1 ? "account uses" : "accounts use"} it.` : "No password is set."}
          </StatusIndicator>
        )}
        <form onSubmit={(event) => { event.preventDefault(); if (password) save.mutate(password, { onSuccess: () => setPassword("") }); }}>
          <SpaceBetween direction="horizontal" size="xs" alignItems="end">
            <FormField label={status.data?.password_set ? "New password" : "Password"}>
              <Input type="password" value={password} autoComplete="new-password" ariaLabel="Account password"
                onChange={({ detail }) => setPassword(detail.value)} />
            </FormField>
            <Button variant="primary" formAction="submit" loading={save.isPending} disabled={!password}>Save password</Button>
          </SpaceBetween>
        </form>
        {save.isSuccess && !password && <Box color="text-status-success">Saved.</Box>}
        {save.isError && <Alert type="error">{save.error.message}</Alert>}
        {status.data?.password_set && accounts > 0 && (
          <Box variant="small" color="text-body-secondary">
            Changing it does not change the {accounts} {accounts === 1 ? "account" : "accounts"} already made with the old one. Sign-in there fails until you reset each to the new password.
          </Box>
        )}
        <Box variant="small" color="text-body-secondary">It needs 8 or more characters with an uppercase letter, a lowercase letter, a digit and a special character.</Box>
      </SpaceBetween>
    </Container>
  );
}

import { useState } from "react";
import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import FormField from "@cloudscape-design/components/form-field";
import Input from "@cloudscape-design/components/input";
import Select from "@cloudscape-design/components/select";
import SpaceBetween from "@cloudscape-design/components/space-between";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import Textarea from "@cloudscape-design/components/textarea";
import { type AnswerMutation, useMutateAnswer } from "@/api/answer-bank";

const NOT_ANSWERED = { value: "", label: "Not answered" };

export interface AnswerRowProps {
  question: string;
  /** The saved or drafted answer shown when the row opens. */
  saved: string;
  scope?: string;
  options?: string[];
  multiline?: boolean;
  /** A draft waiting for the user: saving it unchanged approves it. */
  waiting?: boolean;
  save: (answer: string) => AnswerMutation;
  remove?: () => AnswerMutation;
}

export function AnswerRow({ question, saved, scope, options = [], multiline, waiting, save, remove }: AnswerRowProps) {
  const [value, setValue] = useState(saved);
  const mutation = useMutateAnswer();
  const dirty = value !== saved;
  const choices = [NOT_ANSWERED, ...options.map((option) => ({ value: option, label: option })),
    ...(saved && !options.includes(saved) && options.length ? [{ value: saved, label: saved }] : [])];
  const edit = (next: string) => { mutation.reset(); setValue(next); };
  return <div role="group" aria-label={question}>
    <FormField label={question} description={scope} stretch>
      <SpaceBetween size="xs">
        {options.length > 0
          ? <Select selectedOption={choices.find((choice) => choice.value === value) ?? NOT_ANSWERED} options={choices}
              disabled={mutation.isPending} onChange={({ detail }) => edit(detail.selectedOption.value ?? "")} />
          : multiline
            ? <Textarea value={value} rows={3} disabled={mutation.isPending} onChange={({ detail }) => edit(detail.value)} />
            : <Input value={value} disabled={mutation.isPending} onChange={({ detail }) => edit(detail.value)} />}
        <SpaceBetween direction="horizontal" size="xs" alignItems="center">
          <Button variant={waiting || dirty ? "primary" : "normal"} loading={mutation.isPending} disabled={!value.trim() || !(waiting || dirty)}
            onClick={() => mutation.mutate(save(value))}>Save</Button>
          {remove && <Button disabled={mutation.isPending} onClick={() => mutation.mutate(remove(), { onSuccess: () => setValue("") })}>Delete</Button>}
          {mutation.isSuccess && !dirty && <StatusIndicator type="success">Saved</StatusIndicator>}
          {dirty && !mutation.isPending && <Box color="text-body-secondary" variant="small">Not saved yet</Box>}
        </SpaceBetween>
        {mutation.isError && <StatusIndicator type="error">{mutation.error.message} Reload the page if the answers changed elsewhere.</StatusIndicator>}
      </SpaceBetween>
    </FormField>
  </div>;
}

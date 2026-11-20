import { useState } from "react";
import Alert from "@cloudscape-design/components/alert";
import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import Container from "@cloudscape-design/components/container";
import FormField from "@cloudscape-design/components/form-field";
import Header from "@cloudscape-design/components/header";
import Input from "@cloudscape-design/components/input";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Textarea from "@cloudscape-design/components/textarea";
import Checkbox from "@cloudscape-design/components/checkbox";
import RadioGroup from "@cloudscape-design/components/radio-group";
import Select from "@cloudscape-design/components/select";
import { type AnswerBank, type AnswerEntry, type CommonQuestion, type ScopeFilter, type SourceFilter, answerSource, currentAnswer,
  deleteRequest, matchesSearch, mergeRequest, profileAnswers, sameScope, saveRequest, scopeLabel, useAnswerBank,
  useMutateAnswer } from "@/api/answer-bank";
import { isApplicationsNotInstalled } from "@/api/client";
import { ApplicationsNotSetUp } from "@/components/shell/ApplicationsNotSetUp";
import { AnswerRow } from "./AnswerRow";

const SAVE_NOTE = "Saving an answer approves it. Appliers fill it in when an application asks the same question.";

function BankStatus({ query }: { query: ReturnType<typeof useAnswerBank> }) {
  if (query.isPending) return <Box><div role="status">Loading your answers.</div></Box>;
  if (isApplicationsNotInstalled(query.error)) return <Box><ApplicationsNotSetUp /></Box>;
  if (query.isError) return <Alert type="error" header="Answers could not be loaded" action={<Button onClick={() => { void query.refetch(); }}>Retry answers</Button>}>{query.error.message}</Alert>;
  return null;
}

function CommonRows({ bank, items }: { bank: AnswerBank; items: CommonQuestion[] }) {
  return <SpaceBetween size="l">{items.map((item) => {
    const entry = bank.entries.find((candidate) => candidate.id === item.entry_id);
    return <CommonRow key={item.key} bank={bank} item={item} entry={entry} />;
  })}</SpaceBetween>;
}

function CommonRow({ bank, item, entry }: { bank: AnswerBank; item: CommonQuestion; entry?: AnswerBank["entries"][number] }) {
  const [newId] = useState(() => crypto.randomUUID());
  return <AnswerRow question={item.question} saved={entry ? currentAnswer(entry).answer : ""} options={item.options}
    save={(answer) => saveRequest(bank, answer, entry ? { entry } : { entryId: newId, question: item.question })}
    remove={entry ? () => deleteRequest(bank, entry) : undefined} />;
}

export function PersonalInfoSection() {
  const query = useAnswerBank();
  const items = query.data?.common_questions?.filter((item) => item.group === "demographic") ?? [];
  return <section aria-label="Personal info">
    <Container header={<Header variant="h2" description={`Voluntary self-identification questions that many US employers ask. Choose a decline option to skip one. ${SAVE_NOTE}`}>Personal info</Header>}>
      <SpaceBetween size="l">
        <BankStatus query={query} />
        {query.data && <CommonRows bank={query.data} items={items} />}
      </SpaceBetween>
    </Container>
  </section>;
}

export function ApplicationAnswersSection() {
  const query = useAnswerBank();
  const [added, setAdded] = useState<string[]>([]);
  const bank = query.data;
  const { waiting, saved } = bank ? profileAnswers(bank) : { waiting: [], saved: [] };
  const common = bank?.common_questions?.filter((item) => item.group === "application") ?? [];
  return <section aria-label="Application answers">
    <SpaceBetween size="l">
      <BankStatus query={query} />
      {bank && waiting.length > 0 && <Container header={<Header variant="h2" counter={`(${waiting.length})`}
        description="An Applier drafted these answers. Edit if needed, then save to use them.">Waiting for you</Header>}>
        <SpaceBetween size="l">{waiting.map((entry) => { const answer = currentAnswer(entry); return <AnswerRow key={entry.id} waiting multiline
          question={answer.question} saved={answer.answer} scope={scopeLabel(answer.scope)}
          save={(value) => saveRequest(bank, value, { entry })} remove={() => deleteRequest(bank, entry)} />; })}</SpaceBetween>
      </Container>}
      {bank && <Container header={<Header variant="h2" description={SAVE_NOTE}>Common questions</Header>}>
        <CommonRows bank={bank} items={common} />
      </Container>}
      {bank && <Container header={<Header variant="h2" description="Answers you saved here or gave an Applier."
        actions={<Button iconName="add-plus" onClick={() => setAdded((rows) => [...rows, crypto.randomUUID()])}>Add a question</Button>}>Your answers</Header>}>
        <SpaceBetween size="l">
          {saved.length === 0 && added.length === 0 && <Box color="text-body-secondary">No other saved answers yet.</Box>}
          {saved.length > 0 && <SavedAnswers bank={bank} saved={saved} />}
          {added.map((id) => <NewAnswer key={id} bank={bank} entryId={id} onDone={() => setAdded((rows) => rows.filter((row) => row !== id))} />)}
        </SpaceBetween>
      </Container>}
    </SpaceBetween>
  </section>;
}

function NewAnswer({ bank, entryId, onDone }: { bank: AnswerBank; entryId: string; onDone: () => void }) {
  const [question, setQuestion] = useState("");
  const [answer, setAnswer] = useState("");
  const mutation = useMutateAnswer();
  return <div role="group" aria-label="New question">
    <SpaceBetween size="xs">
      <FormField label="Question" stretch><Input value={question} disabled={mutation.isPending} onChange={({ detail }) => { mutation.reset(); setQuestion(detail.value); }} /></FormField>
      <FormField label="Answer" description="Used for every company." stretch><Textarea value={answer} rows={3} disabled={mutation.isPending} onChange={({ detail }) => { mutation.reset(); setAnswer(detail.value); }} /></FormField>
      <SpaceBetween direction="horizontal" size="xs">
        <Button variant="primary" loading={mutation.isPending} disabled={!question.trim() || !answer.trim()}
          onClick={() => mutation.mutate(saveRequest(bank, answer, { entryId, question }), { onSuccess: onDone })}>Save</Button>
        <Button disabled={mutation.isPending} onClick={onDone}>Cancel</Button>
      </SpaceBetween>
      {mutation.isError && <Alert type="error" header="Answer not saved">{mutation.error.message} Your text is still here.</Alert>}
    </SpaceBetween>
  </div>;
}

const SCOPE_FILTERS: { value: ScopeFilter; label: string }[] = [
  { value: "all", label: "Any scope" }, { value: "global", label: "Every company" },
  { value: "company", label: "One company" }, { value: "job", label: "One job" }];
const SOURCE_FILTERS: { value: SourceFilter; label: string }[] = [
  { value: "all", label: "Any source" }, { value: "user_reply", label: "Your reply" },
  { value: "applier_auto", label: "Applier decided" }, { value: "dashboard", label: "Saved here" }];

function SavedAnswers({ bank, saved }: { bank: AnswerBank; saved: AnswerEntry[] }) {
  const [search, setSearch] = useState("");
  const [scope, setScope] = useState<ScopeFilter>("all");
  const [source, setSource] = useState<SourceFilter>("all");
  const [selectedIds, setSelectedIds] = useState<string[]>([]);
  const shown = saved.filter((entry) => {
    const answer = currentAnswer(entry);
    return matchesSearch(answer, search) && (scope === "all" || answer.scope.kind === scope)
      && (source === "all" || answerSource(answer) === source);
  });
  const selected = saved.filter((entry) => selectedIds.includes(entry.id));
  const toggle = (id: string, on: boolean) => setSelectedIds((ids) => on ? [...ids, id] : ids.filter((other) => other !== id));
  return <SpaceBetween size="l">
    <SpaceBetween direction="horizontal" size="xs">
      <Input type="search" value={search} placeholder="Search questions and answers" ariaLabel="Search saved answers"
        onChange={({ detail }) => setSearch(detail.value)} />
      <Select ariaLabel="Scope" selectedOption={SCOPE_FILTERS.find((option) => option.value === scope) ?? null} options={SCOPE_FILTERS}
        onChange={({ detail }) => setScope(detail.selectedOption.value as ScopeFilter)} />
      <Select ariaLabel="Source" selectedOption={SOURCE_FILTERS.find((option) => option.value === source) ?? null} options={SOURCE_FILTERS}
        onChange={({ detail }) => setSource(detail.selectedOption.value as SourceFilter)} />
    </SpaceBetween>
    {selected.length > 0 && <MergePanel bank={bank} selected={selected} onDone={() => setSelectedIds([])} />}
    {shown.length === 0 && <Box color="text-body-secondary">No saved answers match.</Box>}
    {shown.map((entry) => { const answer = currentAnswer(entry); return <SpaceBetween key={entry.id} size="xxs">
      <Checkbox checked={selectedIds.includes(entry.id)} ariaLabel={`Select ${answer.question}`}
        onChange={({ detail }) => toggle(entry.id, detail.checked)}>Select to merge</Checkbox>
      <AnswerRow multiline question={answer.question} saved={answer.answer} scope={scopeLabel(answer.scope)}
        save={(value) => saveRequest(bank, value, { entry })} remove={() => deleteRequest(bank, entry)} />
    </SpaceBetween>; })}
  </SpaceBetween>;
}

function MergePanel({ bank, selected, onDone }: { bank: AnswerBank; selected: AnswerEntry[]; onDone: () => void }) {
  const [keepId, setKeepId] = useState<string | null>(null);
  const mutation = useMutateAnswer();
  const keep = selected.find((entry) => entry.id === keepId);
  const reason = selected.length < 2 ? "Select at least two answers to merge."
    : !sameScope(selected) ? "These answers apply to different companies or jobs, so they cannot be merged."
      : !keep ? "Choose the answer to keep." : null;
  return <div role="group" aria-label="Merge answers">
    <SpaceBetween size="xs">
      <Box variant="h3">Merge {selected.length} selected</Box>
      <Box color="text-body-secondary">The answer you keep also answers the other questions. The others are removed and keep their history.</Box>
      <FormField label="Answer to keep" stretch>
        <RadioGroup value={keepId} onChange={({ detail }) => { mutation.reset(); setKeepId(detail.value); }}
          items={selected.map((entry) => { const answer = currentAnswer(entry);
            return { value: entry.id, label: answer.question, description: `${answer.answer} (${scopeLabel(answer.scope)})` }; })} />
      </FormField>
      <SpaceBetween direction="horizontal" size="xs" alignItems="center">
        <Button variant="primary" loading={mutation.isPending} disabled={reason !== null}
          onClick={() => keep && mutation.mutate(mergeRequest(bank, keep, selected.filter((entry) => entry !== keep)), { onSuccess: onDone })}>Merge</Button>
        <Button disabled={mutation.isPending} onClick={onDone}>Clear selection</Button>
        {reason && <Box color="text-body-secondary" variant="small">{reason}</Box>}
      </SpaceBetween>
      {mutation.isError && <Alert type="error" header="Answers not merged">{mutation.error.message} Reload the page if the answers changed elsewhere.</Alert>}
    </SpaceBetween>
  </div>;
}

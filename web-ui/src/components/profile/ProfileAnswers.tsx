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
import { type AnswerBank, type CommonQuestion, currentAnswer, deleteRequest, profileAnswers, saveRequest, scopeLabel,
  useAnswerBank, useMutateAnswer } from "@/api/answer-bank";
import { AnswerRow } from "./AnswerRow";

const SAVE_NOTE = "Saving an answer approves it. Appliers fill it in when an application asks the same question.";

function BankStatus({ query }: { query: ReturnType<typeof useAnswerBank> }) {
  if (query.isPending) return <Box><div role="status">Loading your answers.</div></Box>;
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
          {saved.map((entry) => { const answer = currentAnswer(entry); return <AnswerRow key={entry.id} multiline
            question={answer.question} saved={answer.answer} scope={scopeLabel(answer.scope)}
            save={(value) => saveRequest(bank, value, { entry })} remove={() => deleteRequest(bank, entry)} />; })}
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

/** Shown where a page needs the application pipeline, which this Jobfeed does not have. */
export function ApplicationsNotSetUp({ className }: { className?: string }) {
  return (
    <p role="status" className={className}>
      Applying isn't set up on this Jobfeed. It finds and ranks jobs for you. Answering application questions comes
      with the Claude Applier setup.
    </p>
  );
}

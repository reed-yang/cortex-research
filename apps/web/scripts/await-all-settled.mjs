// Awaits every operation, then throws the first one to fail, or returns their
// values in order.
//
// `Promise.all` rejects on the first failure while the other operations still
// run. When one of them is a child process working under a temporary root, the
// caller's `finally` would remove that root under it, and the child's own
// failure would never be reported.
export async function awaitAllSettled(operations) {
  let failed = false;
  let failure;
  const values = await Promise.all(
    operations.map((operation) =>
      Promise.resolve(operation).catch((error) => {
        if (!failed) {
          failed = true;
          failure = error;
        }
      }),
    ),
  );
  if (failed) throw failure;
  return values;
}

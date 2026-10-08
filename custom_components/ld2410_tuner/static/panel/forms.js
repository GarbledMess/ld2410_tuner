// Numeric fields use the same conversion for draft capture and submission.
export function readNumberFields(inputs, key) {
  return Object.fromEntries(
    [...inputs].map((input) => [input.dataset[key], Number(input.value)]),
  );
}

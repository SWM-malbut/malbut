/** Convert D1-style positional placeholders into PostgreSQL placeholders. */
export function postgresPlaceholders(sql: string): string {
  let index = 0;
  let quote: "'" | '"' | null = null;
  let output = "";

  for (let cursor = 0; cursor < sql.length; cursor += 1) {
    const character = sql[cursor];
    if (quote) {
      output += character;
      if (character === quote) {
        if (sql[cursor + 1] === quote) {
          output += sql[cursor + 1];
          cursor += 1;
        } else {
          quote = null;
        }
      }
      continue;
    }

    if (character === "'" || character === '"') {
      quote = character;
      output += character;
      continue;
    }
    if (character === "?") {
      index += 1;
      output += `$${index}`;
      continue;
    }
    output += character;
  }

  return output;
}

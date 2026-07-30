const fs = require('fs');

// Carica il file JSON originale
const array = JSON.parse(fs.readFileSync('./products.json', 'utf8')).filter((el) => !el.toDelete);

const CHUNK_SIZE = 50; // Numero di oggetti per file

for (let i = 0; i < array.length; i += CHUNK_SIZE) {
    // Estrae una fetta di massimo 50 elementi dall'array
    const chunk = array.slice(i, i + CHUNK_SIZE);
    
    // Calcola il numero progressivo del file (1, 2, 3...)
    const fileIndex = Math.floor(i / CHUNK_SIZE) + 1;
    
    // Definisce il percorso del nuovo file
    const outputFilePath = `${__dirname}/files/${fileIndex}_todo.json`;
    
    // Scrive il file JSON
    fs.writeFileSync(outputFilePath, JSON.stringify(chunk, null, 2), 'utf8');
    
    console.log(`Creato file output_${fileIndex}.json con ${chunk.length} oggetti.`);
}
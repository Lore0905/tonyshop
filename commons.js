const axios = require("axios");
const fs = require("fs");
const XLSX = require("xlsx");

const path = require("path");
require("dotenv").config({ path: path.resolve(__dirname, ".env") });

const SHOPIFY_SHOP_DOMAIN = process.env.SHOPIFY_SHOP_DOMAIN;
const SHOPIFY_CLIENT_ID = process.env.SHOPIFY_CLIENT_ID;
const SHOPIFY_CLIENT_SECRET = process.env.SHOPIFY_CLIENT_SECRET;
const API_VERSION = "2024-01";

async function getVendorProducts() {
  const response = await axios.get(
    "https://autofantasy.it/export/Prodotti.xlsx",
    {
      responseType: "arraybuffer",
      headers: {
        "User-Agent":
          "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        Accept:
          "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,application/octet-stream,*/*",
        "Accept-Language": "it-IT,it;q=0.9,en-US;q=0.8,en;q=0.7",
        Referer: "https://autofantasy.it/",
      },
      timeout: 30000,
    },
  );

  const xlsxFilePath = "./files/vendor_products.xlsx";
  const jsonFilePath = "./files/vendor_products.json";

  fs.writeFileSync(xlsxFilePath, Buffer.from(response.data));
  console.log("✅ File scaricato con axios, size:", response.data.length);
  console.log("✅ File xlsx salvato sulla cartella files");

  fs.writeFileSync(
    jsonFilePath,
    JSON.stringify(convertExcelToJson(xlsxFilePath), null, 2),
  );
  console.log("✅ File json salvato sulla cartella files");

  return JSON.parse(fs.readFileSync(jsonFilePath, "utf8"));
}

async function validateShopifyToken() {
  console.log("Shop domain:", SHOPIFY_SHOP_DOMAIN);
  console.log("Client ID:", SHOPIFY_CLIENT_ID ? "presente" : "MANCANTE");
  console.log(
    "Client Secret:",
    SHOPIFY_CLIENT_SECRET ? "presente" : "MANCANTE",
  );

  const tokenUrl = `https://${SHOPIFY_SHOP_DOMAIN}/admin/oauth/access_token`;

  const tokenResponse = await axios.post(
    tokenUrl,
    new URLSearchParams({
      grant_type: "client_credentials",
      client_id: SHOPIFY_CLIENT_ID,
      client_secret: SHOPIFY_CLIENT_SECRET,
    }).toString(),
    {
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
    },
  );

  const accessToken = tokenResponse.data.access_token;
  console.log("✅ Token ottenuto:", accessToken);

  // 2. Valida il token con una query GraphQL semplice
  const testResponse = await axios.post(
    `https://${SHOPIFY_SHOP_DOMAIN}/admin/api/2024-01/graphql.json`,
    { query: "{ shop { name id } }" },
    {
      headers: {
        "X-Shopify-Access-Token": accessToken,
        "Content-Type": "application/json",
      },
    },
  );

  console.log("✅ Token valido! Shop:", testResponse.data.data.shop.name);
  return accessToken;
}

async function getShopifyProducts() {
  const accessToken = await validateShopifyToken();

  const query = `
    query GetProductVariants($first: Int!, $after: String) {
      productVariants(first: $first, after: $after) {
        edges {
          node {
            sku
            inventoryItem {
              id
            }
            inventoryQuantity
          }
        }
        pageInfo {
          hasNextPage
          endCursor
        }
      }
    }
  `;

  const products = {};
  let hasNextPage = true;
  let after = null;

  while (hasNextPage) {
    const response = await axios.post(
      `https://${SHOPIFY_SHOP_DOMAIN}/admin/api/2024-01/graphql.json`,
      {
        query,
        variables: { first: 250, after },
      },
      {
        headers: {
          "X-Shopify-Access-Token": accessToken,
          "Content-Type": "application/json",
        },
      },
    );

    if (response.data.errors) {
      throw new Error(response.data.errors[0].message);
    }

    const { edges, pageInfo } = response.data.data.productVariants;

    for (const edge of edges) {
      const variant = edge.node;
      if (variant.sku) {
        products[variant.sku] = {
          inventoryItemId: variant.inventoryItem.id,
          quantity: variant.inventoryQuantity,
        };
      }
    }

    hasNextPage = pageInfo.hasNextPage;
    after = pageInfo.endCursor;
  }

  console.log(
    `📦 Caricate ${Object.keys(products).length} varianti da Shopify`,
  );
  fs.writeFileSync("./files/shopify_products.json", JSON.stringify(products));

  return products;
}

function convertExcelToJson(filePath) {
  // 1. Legge il file Excel
  const workbook = XLSX.readFile(filePath);

  // 2. Prende il nome del primo foglio (solitamente "Sheet1" o simile)
  const sheetName = workbook.SheetNames[0];

  // 3. Converte il foglio in JSON
  const jsonData = XLSX.utils.sheet_to_json(workbook.Sheets[sheetName]);

  return jsonData;
}

function chunkArray(array, chunkSize = 250) {
  const chunks = [];

  for (let i = 0; i < array.length; i += chunkSize) {
    chunks.push(array.slice(i, i + chunkSize));
  }

  return chunks;
}

async function updateShopifyInventory(
  updates,
  locationId = process.env.DEFAULT_LOCATION_ID,
) {
  console.log(`location id ${process.env.DEFAULT_LOCATION}`);

  const accessToken = await validateShopifyToken();

  const updateSlitted = chunkArray(updates);

  if (!Array.isArray(updates) || updates.length === 0) {
    throw new Error("❌ Devi passare un array di aggiornamenti");
  }
  if (!locationId) {
    throw new Error(
      "❌ Manca locationId. Passalo come parametro o inseriscilo in SHOPIFY_LOCATION_ID",
    );
  }

  const mutation = `
    mutation inventorySetQuantities($input: InventorySetQuantitiesInput!) {
      inventorySetQuantities(input: $input) {
        inventoryAdjustmentGroup {
          id
          reason
          changes {
            name
            delta
            quantityAfterChange
          }
        }
        userErrors {
          field
          message
        }
      }
    }
  `;

  for (let ix = 0; ix < updateSlitted.length; ix++) {
    const updates = updateSlitted[ix];

    const variables = {
      input: {
        name: "available",
        reason: "correction",
        ignoreCompareQuantity: true,
        quantities: updates.map((u) => ({
          inventoryItemId: u.inventoryItemId,
          locationId: locationId,
          quantity: u.quantity,
        })),
      },
    };

    try {
      const response = await axios.post(
        `https://${SHOPIFY_SHOP_DOMAIN}/admin/api/2024-01/graphql.json`,
        { query: mutation, variables },
        {
          headers: {
            "X-Shopify-Access-Token": accessToken,
            "Content-Type": "application/json",
          },
        },
      );

      // Errori GraphQL generali (es. query malformata, permessi mancanti)
      if (response.data.errors) {
        const messages = response.data.errors.map((e) => e.message).join(" | ");
        throw new Error(`GraphQL Error: ${messages}`);
      }

      const result = response.data.data.inventorySetQuantities;
      const userErrors = result.userErrors || [];
      const inventoryLevels = result.inventoryLevels || [];

      const report = {
        success: userErrors.length === 0,
        totalRequested: updates.length,
        updated: updates.length - userErrors.length,
        failed: userErrors.length,
        errors: [],
        updatedItems: [],
      };

      // Logga gli aggiornamenti riusciti
      inventoryLevels.forEach((level) => {
        const qty = level.quantities?.[0]?.quantity;
        report.updatedItems.push({ inventoryLevelId: level.id, quantity: qty });
      });

      // Logga gli errori specifici per item
      if (userErrors.length > 0) {
        userErrors.forEach((err, i) => {
          // Cerca di risalire all'item dall'indice del campo (es. "quantities[5].inventoryItemId")
          const match = err.field?.match(/quantities\[(\d+)\]/);
          const index = match ? parseInt(match[1]) : null;
          const relatedItem = index !== null ? updates[index] : null;

          const errorEntry = {
            index: index,
            inventoryItemId: relatedItem?.inventoryItemId || "sconosciuto",
            requestedQuantity: relatedItem?.quantity,
            field: err.field,
            message: err.message,
          };

          report.errors.push(errorEntry);
          console.error(
            `❌ [${i + 1}] Errore item: ${errorEntry.inventoryItemId}`,
          );
          console.error(`   Campo: ${err.field} | Messaggio: ${err.message}`);
        });
      }

      console.log(
        `📊 Report: ${report.updated}/${report.totalRequested} aggiornati | ${report.failed} falliti`,
      );
    } catch (error) {
      if (error.response) {
        // Errore HTTP (401, 429 rate limit, 500, ecc.)
        const status = error.response.status;
        const detail = JSON.stringify(error.response.data, null, 2);
        console.error(`❌ Errore HTTP ${status}:`);
        console.error(detail);
        throw new Error(`Shopify HTTP ${status}: ${detail}`);
      }
      // Altri errori (rete, timeout, ecc.)
      console.error("❌ Errore durante l'aggiornamento:", error.message);
      throw error.message;
    }
  }
}

async function shopifyGraphQL(query, variables = {}, retries = 3) {
  const url = `https://${SHOPIFY_SHOP_DOMAIN}/admin/api/${API_VERSION}/graphql.json`;

  const accessToken = await validateShopifyToken();

  const response = await fetch(url, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-Shopify-Access-Token": accessToken,
    },
    body: JSON.stringify({ query, variables }),
  });

  // Rate limiting: Shopify risponde 429
  if (response.status === 429 && retries > 0) {
    const delay = 1000 * (4 - retries); // 1s, 2s, 3s
    console.warn(`⏳ Rate limit (429). Retry tra ${delay}ms...`);
    await new Promise((r) => setTimeout(r, delay));
    return shopifyGraphQL(query, variables, retries - 1);
  }

  if (!response.ok) {
    const text = await response.text();
    throw new Error(`HTTP ${response.status}: ${text}`);
  }

  const data = await response.json();

  // Errori GraphQL nel body (es. permessi, campi invalidi)
  if (data.errors) {
    const messages = data.errors.map((e) => e.message).join(" | ");
    throw new Error(`GraphQL Error: ${messages}`);
  }

  return data;
}

async function findBySku(sku) {
  const q = `
    query($query: String!) {
      products(first: 1, query: $query) {
        edges {
          node {
            id
            title
            status
            variants(first: 1) {
              edges {
                node { sku }
              }
            }
          }
        }
      }
    }
  `;
  const data = await shopifyGraphQL(q, { query: `sku:${sku}` });
  console.log(data);
  return data.data?.products?.edges?.[0]?.node;
}

module.exports = {
  getVendorProducts,
  getShopifyProducts,
  updateShopifyInventory,
  validateShopifyToken,
  findBySku,
  shopifyGraphQL
};

import hashlib
import time
from typing import List

import dagster as dg
from dagster_openai import OpenAIResource
from langchain_core.documents import Document

from project_ask_ai_dagster.resources.github import GithubResource
from project_ask_ai_dagster.resources.pinecone import PineconeResource
from project_ask_ai_dagster.resources.scraper import SitemapScraper

START_TIME = "2023-01-01"
weekly_partition = dg.WeeklyPartitionsDefinition(start_date=START_TIME)


@dg.asset(
    group_name="ingestion",
    kinds={"github"},
    partitions_def=weekly_partition,
    io_manager_key="document_io_manager",
    automation_condition=dg.AutomationCondition.on_cron("0 0 * * 1"),
    description="""
   Ingests raw GitHub issues data from the Dagster repository on a weekly basis.
   
   This asset fetches GitHub issues, including:
   - Issue title and body
   - Comments and discussion threads
   - Issue metadata (status, labels, assignees)
   - Creation and update timestamps
   
   Technical Details:
       - Runs weekly (Mondays at midnight)
       - Processes issues in weekly partitions
       - Converts issues to Document format for embedding
       - Preserves all issue metadata for search context
       
   Returns:
       List[Document]: Collection of Document objects containing issue content 
       and associated metadata for each weekly partition
   """,
)
def github_issues_raw(
    context: dg.AssetExecutionContext,
    github: GithubResource,
) -> List[Document]:
    start, end = context.partition_time_window
    context.log.info(f"Finding issues from {start} to {end}")

    issues = github.get_issues(
        start_date=start.strftime("%Y-%m-%d"), end_date=end.strftime("%Y-%m-%d")
    )

    return github.convert_issues_to_documents(issues)


@dg.asset(
    group_name="embeddings",
    kinds={"github", "openai", "pinecone"},
    partitions_def=weekly_partition,
    io_manager_key="document_io_manager",
    automation_condition=dg.AutomationCondition.any_deps_updated(),
    description="""
   Creates and stores vector embeddings for GitHub issues in Pinecone.
   
   This asset processes weekly batches of GitHub issues by:
   1. Converting issue content to OpenAI embeddings
   2. Storing embeddings and metadata in Pinecone vector database
   3. Using namespace 'dagster-github' for unified GitHub content storage
   
   Dependencies:
       - github_issues_raw: Raw issue documents from weekly partition
       
   Technical Details:
       - Uses OpenAI's text-embedding-3-small model
       - Embedding dimension: 1536
       - Stores in Pinecone index: 'dagster-knowledge'
       - Preserves metadata like issue status, labels, and timestamps
       - Processes issues in weekly batches
       
   Vector Storage:
       - Each vector contains issue content embedding and metadata
       - Uses auto-generated sequential IDs
       - Stored in 'dagster-github' namespace for consolidated search
       
   Returns:
       MaterializeResult with metadata about number of issues processed
   """,
)
def github_issues_embeddings(
    context: dg.AssetExecutionContext,
    openai: OpenAIResource,
    pinecone: PineconeResource,
    github_issues_raw: List[Document],
) -> dg.MaterializeResult:
    # Create index if doesn't exist
    pinecone.create_index("dagster-knowledge", dimension=1536)
    index, namespace_kwargs = pinecone.get_index("dagster-knowledge", namespace="dagster-github")

    texts = [doc.page_content for doc in github_issues_raw]
    with openai.get_client(context) as client:
        embeddings = [
            item.embedding
            for item in client.embeddings.create(model="text-embedding-3-small", input=texts).data
        ]
    # Prepare metadata
    metadata = [
        {k: v for k, v in doc.metadata.items() if isinstance(v, (str, int, float, bool))}
        for doc in github_issues_raw
    ]

    # Upsert to Pinecone with namespace
    index.upsert(
        vectors=zip(
            [str(i) for i in range(len(texts))],  # IDs
            embeddings,
            metadata,
        ),
        **namespace_kwargs,  # Include namespace parameters
    )

    return dg.MaterializeResult(
        metadata={
            "number_of_issues": len(github_issues_raw),
        }
    )


@dg.asset(
    group_name="ingestion",
    kinds={"github"},
    partitions_def=weekly_partition,
    io_manager_key="document_io_manager",
    automation_condition=dg.AutomationCondition.on_cron("0 0 * * 1"),
    description="""
   Retrieves GitHub discussions within a date range and converts them to Document objects.
   
   This asset runs weekly to fetch discussions from the Dagster GitHub repository.
   It converts each discussion into a Document object containing the discussion content
   and metadata like title, URL, and creation date.
   
   Returns:
       List[Document]: List of Document objects containing discussion content and metadata
       
   Schedule:
       Runs weekly on Monday at midnight (0 0 * *1)
       
   Partitioning:
       Uses weekly partitions to process discussions by date range
   """,
)
def github_discussions_raw(
    context: dg.AssetExecutionContext,
    github: GithubResource,
) -> List[Document]:
    start, end = context.partition_time_window
    context.log.info(f"Finding discussions from {start} to {end}")

    discussions = github.get_discussions(
        start_date=start.strftime("%Y-%m-%d"), end_date=end.strftime("%Y-%m-%d")
    )

    return github.convert_discussions_to_documents(discussions)


@dg.asset(
    group_name="embeddings",
    kinds={"github", "openai", "pinecone"},
    partitions_def=weekly_partition,
    io_manager_key="document_io_manager",
    automation_condition=dg.AutomationCondition.any_deps_updated(),
    description="""
   Creates vector embeddings from GitHub discussions and stores them in Pinecone.
   
   This asset processes GitHub discussions by:
   1. Converting discussion text into OpenAI embeddings
   2. Storing embeddings and metadata in Pinecone vector database
   3. Using namespace 'dagster-github' for discussions content
   
   Dependencies:
       - github_discussions_raw: Raw discussion documents to embed
       
   Technical Details:
       - Uses OpenAI's text-embedding-3-small model
       - Embeddings dimension: 1536
       - Stores in Pinecone index: 'dagster-knowledge'
       - Includes metadata like title, URL, and creation date
       
   Partitioning:
       Uses weekly partitions to process discussions in batches
       
   Returns:
       MaterializeResult with metadata about number of discussions processed
   """,
)
def github_discussions_embeddings(
    context: dg.AssetExecutionContext,
    openai: OpenAIResource,
    pinecone: PineconeResource,
    github_discussions_raw: List[Document],
) -> dg.MaterializeResult:
    BATCH_SIZE = 20

    # Create index if doesn't exist
    pinecone.create_index("dagster-knowledge", dimension=1536)
    index, namespace_kwargs = pinecone.get_index("dagster-knowledge", namespace="dagster-github")

    all_texts = [doc.page_content for doc in github_discussions_raw]
    all_embeddings = []

    with openai.get_client(context) as client:
        # Process in batches
        for i in range(0, len(all_texts), BATCH_SIZE):
            batch_texts = all_texts[i : i + BATCH_SIZE]
            batch_embeddings = [
                item.embedding
                for item in client.embeddings.create(
                    model="text-embedding-3-small", input=batch_texts
                ).data
            ]
            all_embeddings.extend(batch_embeddings)
            time.sleep(1)

    # Prepare metadata
    metadata = [
        {k: v for k, v in doc.metadata.items() if isinstance(v, (str, int, float, bool))}
        for doc in github_discussions_raw
    ]

    # Upsert to Pinecone with namespace
    index.upsert(
        vectors=zip(
            [str(i) for i in range(len(all_texts))],
            all_embeddings,
            metadata,
        ),
        **namespace_kwargs,
    )

    return dg.MaterializeResult(
        metadata={
            "number_of_discussions": len(github_discussions_raw),
        }
    )


# Webscraping asset
@dg.asset(
    group_name="ingestion",
    kinds={"webscraping"},
    io_manager_key="document_io_manager",
    automation_condition=dg.AutomationCondition.on_cron(
        "0 0 * * 1"
    ),  # weekly on monday at midnight
    description="""
   Scrapes documentation pages from Dagster's documentation site and converts them to Documents.
   
   This asset:
   1. Fetches URLs from the Dagster documentation sitemap
   2. Processes the first 4 URLs as a sample set
   3. Converts each page into a Document object with cleaned content
   4. Implements rate limiting (0.5s delay between requests)
   
   Technical Details:
       - Uses BeautifulSoup for HTML parsing
       - Removes boilerplate elements (scripts, styles, nav, etc.)
       - Preserves main content and article sections
       - Includes metadata like page title and source URL
       
   Rate Limiting:
       - 0.5 second delay between requests to avoid server overload
       - Processes pages sequentially
       
   Schedule:
       Runs weekly on Monday at midnight (0 0 * *1)
   
   Returns:
       List[Document]: Collection of processed Document objects containing
       page content and metadata
       
   Output Metadata:
       - Number of pages scraped
   """,
)
def docs_scrape_raw(
    context: dg.AssetExecutionContext,
    scraper: SitemapScraper,
) -> List[Document]:
    urls = scraper.parse_sitemap()[0:4]
    documents = []
    # Scrape each URL
    for i, url in enumerate(urls, 1):
        doc = scraper.scrape_page(url)
        if doc:
            documents.append(doc)
        # Add delay between requests
        time.sleep(0.5)

    context.add_output_metadata({"pages scraped": len(urls)})

    return documents


@dg.asset(
    group_name="embeddings",
    kinds={"webscraping", "pinecone", "openai"},
    automation_condition=dg.AutomationCondition.eager(),
    io_manager_key="document_io_manager",
    description="""
   Creates vector embeddings from scraped documentation pages and stores them in Pinecone.
   
   This asset processes scraped documentation by:
   1. Converting document content into OpenAI embeddings
   2. Processing and cleaning document metadata
   3. Creating unique document IDs using MD5 hashes of URLs
   4. Storing embeddings and metadata in Pinecone vector database
   
   Dependencies:
       - docs_scrape_raw: Raw documentation pages to embed
       
   Technical Details:
       - Uses OpenAI's text-embedding-3-small model
       - Embeddings dimension: 1536
       - Stores in Pinecone index: 'dagster-knowledge'
       - Uses 'dagster-docs' namespace
       - Generates MD5 hash IDs from source URLs
       
   Storage Details:
       - Each vector contains:
           - Document embedding
           - Cleaned metadata (strings, ints, floats, bools only)
           - Unique ID based on URL
       - Stored in batch operations for efficiency
       
   Returns:
       MaterializeResult containing:
       - Number of documents embedded
       - Embedding dimension size
       - List of processed URLs
   """,
)
def docs_embedding(
    context: dg.AssetExecutionContext,
    pinecone: PineconeResource,
    openai: OpenAIResource,
    docs_scrape_raw: List[Document],
) -> dg.MaterializeResult:
    pinecone.create_index("dagster-knowledge", dimension=1536)
    index, namespace_kwargs = pinecone.get_index("dagster-knowledge", namespace="dagster-docs")

    # Get embeddings for all documents
    with openai.get_client(context) as client:
        texts = [doc.page_content for doc in docs_scrape_raw]
        embeddings_response = client.embeddings.create(model="text-embedding-3-small", input=texts)
        embeddings = [item.embedding for item in embeddings_response.data]

    # Prepare metadata for each document
    metadatas = []
    doc_ids = []

    for doc in docs_scrape_raw:
        # Clean metadata
        meta = {k: v for k, v in doc.metadata.items() if isinstance(v, (str, int, float, bool))}
        metadatas.append(meta)

        # Create unique ID for each document
        doc_id = hashlib.md5(doc.metadata["source"].encode()).hexdigest()
        doc_ids.append(doc_id)

    # Batch upsert to Pinecone
    index.upsert(vectors=zip(doc_ids, embeddings, metadatas), **namespace_kwargs)

    return dg.MaterializeResult(
        metadata={
            "documents_embedded": len(docs_scrape_raw),
            "embedding_dimension": len(embeddings[0]) if embeddings else 0,
            "urls_processed": [doc.metadata["source"] for doc in docs_scrape_raw],
        }
    )
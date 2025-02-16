import re
import time
from io import BytesIO
from typing import List

import requests
from PyPDF2 import PdfReader
from edgar import Company, set_identity
from instructor import patch
from langchain_text_splitters import TokenTextSplitter
from openai import OpenAI
from tqdm import tqdm

from financial_datasets.dataset import DatasetItem, Dataset
from financial_datasets.filings import filter_filings
from financial_datasets.prompts import default_prompt

default_sec_identity = "gary gary@financialdatasets.org"


class DatasetGenerator:
    def __init__(self, model: str, api_key: str):
        # Ensure model begins with gpt-
        if not model.startswith('gpt-'):
            raise NotImplementedError(f'Model {model} is not supported yet.')

        if not api_key:
            raise ValueError("API key is required.")

        self._model = model
        self._client = patch(OpenAI(api_key=api_key))

    def generate_from_texts(
        self,
        texts: List[str],
        max_questions=10,
    ) -> Dataset:
        """
        Generate questions from a list of texts.

        :param texts: List of texts to generate questions from.
        :param max_questions: Maximum number of questions to generate.
        :return: Dataset containing the generated questions.
        """
        items: List[DatasetItem] = []
        num_texts = len(texts)
        questions_per_text = max_questions // num_texts
        remaining_questions = max_questions % num_texts

        progress_bar = tqdm(total=max_questions, desc="Generating questions", colour='green')

        for index, text in enumerate(texts):
            try:
                # Determine the number of questions to generate for the current text
                current_max_questions = questions_per_text
                if index < remaining_questions:
                    current_max_questions += 1

                # Generate questions
                response = self._client.chat.completions.create(
                    model=self._model,
                    response_model=Dataset,
                    messages=[
                        {"role": "system", "content": default_prompt},
                        {"role": "user", "content": f"Generate {current_max_questions} questions for the following block of text: {text}"}
                    ],
                )

                # Add the generated items to our total list of questions
                items.extend(response.items)

                # Update the progress bar by the number of questions generated
                progress_bar.update(len(response.items))

                # Stop generating questions if we have reached the maximum number of questions
                if len(items) == max_questions:
                    break

            except Exception as e:
                print(f"Failed to generate questions for batch {index + 1}: {e}")
                continue

            # Sleep for 1 second to avoid overloading the LLM
            time.sleep(1)

        # Ensure the progress bar is closed
        progress_bar.close()

        return Dataset(
            items=items,
        )

    def generate_from_pdf(
        self,
        url: str,
        max_questions=10,
        **kwargs,
    ) -> Dataset:
        """
        Generate questions from a PDF file.

        :param url: The URL of the PDF file.
        :param max_questions: Maximum number of questions to generate.
        :param kwargs: Additional arguments like chunk_size, chunk_overlap, etc.
        :return: Dataset containing the generated questions.
        """
        # Download the PDF file
        response = requests.get(url)
        pdf_file = BytesIO(response.content)

        # Extract text from the PDF file
        reader = PdfReader(pdf_file)
        text = ""
        for page in reader.pages:
            text += page.extract_text()

        # Remove any newline characters
        text = text.replace("\n", " ")

        # Chunk the text to prevent exceeding the context window of models at the question generation step.
        chunk_size = kwargs.get("chunk_size", 1024)
        chunk_overlap = kwargs.get("chunk_overlap", 128)

        # Split by tokens
        token_splitter = TokenTextSplitter(
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
        )

        # Chunk the text
        texts = token_splitter.split_text(text)

        return self.generate_from_texts(texts=texts, max_questions=max_questions)

    def generate_from_10K(
        self,
        ticker: str,
        year: int,
        max_questions=10,
        sec_identity=default_sec_identity,
    ) -> Dataset:
        """
        Generate questions from a specific SEC filing for a given ticker.

        :param ticker: The stock ticker symbol.
        :param year: The year of the filing.
        :param max_questions: Maximum number of questions to generate.
        :param sec_identity: The identity to use when making requests to the SEC API.
        :return: Dataset containing the generated questions.
        """

        # Tell the SEC who is making the request
        set_identity(sec_identity)

        if not ticker:
            raise ValueError("Ticker symbol is required.")

        if not year:
            raise ValueError("Year is required.")

        # Create a Company object
        company = Company(ticker)

        # Retrieve the SEC filing
        filings = company.get_filings(form="10-K")

        filing = filter_filings(filings, "10-K", year).obj()
        items = [filing[item] for item in filing.items if len(filing[item]) > 200]  # Ignore short items like "Item 6. Reserved"

        # Remove any newline characters
        items = [item.replace("\n", " ") for item in items]

        # Remove any sequence of 3 or more '-' or '.' characters
        pattern1 = r'-{3,}|\.{3,}'
        items = [re.sub(pattern1, '', item) for item in items]

        # Remove any sequence of 2 or more '+' characters
        pattern2 = r'\+{2,}'
        items = [re.sub(pattern2, '', item) for item in items]

        # Chunk Items to prevent exceeding the context window of models at the question generation step.
        chunk_size = 8192
        chunk_overlap = 128
        token_splitter = TokenTextSplitter(chunk_size=chunk_size, chunk_overlap=chunk_overlap)
        texts = []  # List to hold the chunked items
        for item in items:
            chunks = token_splitter.split_text(item)
            texts.extend(chunks)

        # Generate questions from the extracted text
        return self.generate_from_texts(texts=texts, max_questions=max_questions)

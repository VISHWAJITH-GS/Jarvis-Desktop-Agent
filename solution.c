#define _GNU_SOURCE // For getline

#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include <limits.h>

// Function to compute absolute difference
int absolute(int a, int b) {
    return (a > b) ? (a - b) : (b - a);
}

int main() {
    char *S = NULL, *W1 = NULL, *W2 = NULL;
    size_t lenS = 0, lenW1 = 0, lenW2 = 0;

    // Read input S, W1, W2 faithfully using getline
    if (getline(&S, &lenS, stdin) == -1) return 1;
    if (getline(&W1, &lenW1, stdin) == -1) return 1;
    if (getline(&W2, &lenW2, stdin) == -1) return 1;

    // Remove potential trailing newline characters
    S[strcspn(S, "\n")] = 0;
    S[strcspn(S, "\r")] = 0;
    W1[strcspn(W1, "\n")] = 0;
    W1[strcspn(W1, "\r")] = 0;
    W2[strcspn(W2, "\n")] = 0;
    W2[strcspn(W2, "\r")] = 0;

    // Remove any trailing spaces from input words if present
    // Simple strtok approach to split string S by space
    char *tempS = strdup(S); // Duplicate since strtok modifies original
    char *word = strtok(tempS, " ");
    
    char *words[200]; // Boundary condition implies max 200 character string length
    int wordPositions[200];
    int wordCount = 0;
    int currentOffset = 0;

    // Tokenize original string S by space
    // Since strtok compresses delimiters, we need custom logic to find original index
    // However, the problem just asks for minimum distance between W1 and W2.
    // Let's assume input format strictly follows words separated by spaces.
    
    free(tempS);
    tempS = strdup(S);
    word = strtok(tempS, " ");

    int minDistance = INT_MAX;
    int lastW1 = -1, lastW2 = -1;
    int pos = 1; // Position could mean word count position or character index.
                 // Question says "minimum distance", typically means |pi - pj|. Let's use character index of start of word.

    // Better approach: parse original string char by char to find full words and their starting positions.
    int i = 0;
    int currentPos = 0; // Using starting character index.
    int n = strlen(S);
    
    while(i < n) {
        // Skip spaces
        while(i < n && S[i] == ' ') {
            i++;
        }
        if(i == n) break;
        
        int start = i;
        // Find end of word
        while(i < n && S[i] != ' ') {
            i++;
        }
        int end = i;
        int wordLen = end - start;
        char *currentWord = strndup(S + start, wordLen);

        if(strcmp(currentWord, W1) == 0) {
            lastW1 = start;
            if(lastW2 != -1) {
                int dist = absolute(lastW1, lastW2);
                if(dist < minDistance) minDistance = dist;
            }
        }
        if(strcmp(currentWord, W2) == 0) {
            lastW2 = start;
            if(lastW1 != -1) {
                int dist = absolute(lastW1, lastW2);
                if(dist < minDistance) minDistance = dist;
            }
        }
        free(currentWord);
    }
    
    // Print distance. Minimum of |p1-p2|
    printf("%d", minDistance);

    free(S);
    free(W1);
    free(W2);
    free(tempS);

    return 0;
}
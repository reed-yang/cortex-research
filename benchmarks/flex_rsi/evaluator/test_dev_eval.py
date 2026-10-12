"""Grader checks, not candidate solution tests. Public only."""
import unittest
from dev_eval import chunk_cases, grade_chunks, reference


class GraderChecks(unittest.TestCase):
    def test_known_good_ranges_and_shortcuts(self):
        case = chunk_cases()[0]
        good = [dict(id=str(i),section=s,start=a,end=b)
                for i,(a,b,s) in enumerate(case['spans'])]
        self.assertTrue(grade_chunks(case,good))
        self.assertFalse(grade_chunks(case,[]))
        self.assertFalse(grade_chunks(case,good+good))
        self.assertFalse(grade_chunks(case,[dict(id='all',section='Method',start=0,
                                                end=len(case['document'].encode()))]))
        corrupt = [dict(row) for row in good]
        corrupt[0]['section'] = 'References'
        self.assertFalse(grade_chunks(case,corrupt))
    def test_reference_convergence(self):
        parameters = (1.7,.7,7.8,1.2)
        low = reference(parameters,[.1,.3],2048)
        high = reference(parameters,[.1,.3],4096)
        self.assertLess(max(abs(a-b) for x,y in zip(low,high) for a,b in zip(x,y)),1e-8)


if __name__ == '__main__':
    unittest.main()
